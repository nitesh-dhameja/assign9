import os
from dotenv import load_dotenv
import torch
import torch.optim as optim
import torch.nn as nn
import torchvision.transforms as transforms
import torchvision.datasets as datasets
from torch.utils.data import DataLoader, Dataset
from models.resnet_model import ResNet50
from tqdm import tqdm
import logging
import boto3
from PIL import Image
import io
from torchsummary import summary
from datetime import datetime
import pyarrow.parquet as pq
import pyarrow as pa

# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class S3ImageNetDataset(Dataset):
    def __init__(self, bucket_name, transform=None, is_train=True):
        """
        Dataset for loading ImageNet from S3 with streaming support
        """
        self.bucket_name = bucket_name
        self.transform = transform
        self.is_train = is_train
        self.s3_client = boto3.client('s3')
        
        # Determine the directory (train or validation)
        self.data_dir = 'imagenet/train' if is_train else 'imagenet/validation'
        logging.info(f"Loading dataset from s3://{bucket_name}/{self.data_dir}")
        
        # List all available directories
        self.discover_structure()
        
    def discover_structure(self):
        """
        Discover the dataset structure in S3 without loading data into memory
        """
        try:
            # List contents of the data directory
            paginator = self.s3_client.get_paginator('list_objects_v2')
            prefix = f"{self.data_dir}/"
            
            # Get all Arrow files
            self.arrow_files = []
            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
                for obj in page.get('Contents', []):
                    if obj['Key'].endswith('.arrow'):
                        self.arrow_files.append(obj['Key'])
            
            logging.info(f"Found {len(self.arrow_files)} Arrow files")
            
            # Load metadata and sample data from each file
            self.file_sizes = []  # Store size of each file
            self.cumulative_sizes = [0]  # For indexing into correct file
            total_samples = 0
            all_labels = set()
            
            for arrow_file in tqdm(self.arrow_files, desc="Loading dataset structure"):
                try:
                    # Get file metadata using head_object
                    head = self.s3_client.head_object(
                        Bucket=self.bucket_name,
                        Key=arrow_file
                    )
                    file_size = head['ContentLength']
                    
                    # Get the object
                    response = self.s3_client.get_object(
                        Bucket=self.bucket_name,
                        Key=arrow_file
                    )
                    
                    # Read and process the file in chunks
                    stream = pa.ipc.open_stream(response['Body'])
                    
                    # Get the first batch to sample labels
                    batch = next(stream)
                    if batch is not None:
                        if 'label' in batch.schema.names:
                            labels = batch['label'].to_numpy()
                            all_labels.update(labels)
                        
                        # Count records in this batch
                        num_records = len(batch)
                        
                        # Store file information
                        self.file_sizes.append(num_records)
                        total_samples += num_records
                        self.cumulative_sizes.append(total_samples)
                        
                        logging.info(f"Processed {arrow_file}: found {num_records} records")
                    
                except Exception as e:
                    logging.error(f"Error processing file {arrow_file}: {str(e)}")
                    continue
            
            if not all_labels:
                raise ValueError("No valid labels found in the dataset")
            
            if not self.file_sizes:
                raise ValueError("No valid Arrow files could be processed")
            
            # Create label mapping
            self.class_to_idx = {label: idx for idx, label in enumerate(sorted(all_labels))}
            logging.info(f"Found {total_samples} samples with {len(self.class_to_idx)} classes")
            
        except Exception as e:
            logging.error(f"Error discovering dataset structure: {str(e)}")
            raise
    
    def __len__(self):
        return self.cumulative_sizes[-1]
    
    def __getitem__(self, idx):
        # Find which file contains this index
        file_idx = next(i for i, size in enumerate(self.cumulative_sizes[1:], 1) 
                       if idx < size) - 1
        local_idx = idx - self.cumulative_sizes[file_idx]


        response = None
        retries = 3
        for _ in range(retries):
            try:
                # Get the file
                response = self.s3_client.get_object(
                    Bucket=self.bucket_name,
                    Key=self.arrow_files[file_idx]
                )
                break  # Exit the loop if successful
            except Exception as e:
                logging.warning(f"Retrying due to error: {e}")

        if response is None:
            raise RuntimeError(f"Failed to fetch file {self.arrow_files[file_idx]} after {retries} retries")
                
        try:    
            # Read the file
            stream = pa.ipc.open_stream(response['Body'])
            
            # Skip to the correct batch
            current_idx = 0
            for batch in stream:
                try:
                    if batch is not None:
                        batch_size = len(batch)
                        if current_idx + batch_size > local_idx:
                            # Found the correct batch
                            record_idx = local_idx - current_idx
                            image_data = batch['image'][record_idx]['bytes'].as_buffer()
                            label = batch['label'][record_idx].as_py()
                            
                            # Convert to PIL Image
                            image = Image.open(io.BytesIO(image_data)).convert('RGB')
                            
                            # Apply transforms
                            if self.transform:
                                image = self.transform(image)
                            
                            return image, label
                        current_idx += batch_size
                except Exception as e:
                    logging.error(f"Error reading batch: {e}")
                    continue  # Skip to the next batch if there's an error        
            
            raise ValueError(f"Could not find record at index {idx}")
            
        except Exception as e:
            logging.error(f"Error loading record at index {idx} from file {self.arrow_files[file_idx]}: {str(e)}")
            raise

def save_training_log(epoch, train_loss, train_acc, val_loss, val_acc, timestamp):
    """
    Save training logs to a markdown file
    """
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"training_log_{timestamp}.md")
    
    # Create or append to the log file
    mode = 'a' if os.path.exists(log_file) else 'w'
    with open(log_file, mode) as f:
        if mode == 'w':
            # Write header if new file
            f.write("# ImageNet Training Log\n\n")
            f.write("| Epoch | Train Loss | Train Acc | Val Loss | Val Acc | Top-1 Target Met |\n")
            f.write("|-------|------------|-----------|----------|----------|------------------|\n")
        
        # Add row with top-1 accuracy check
        target_met = "✅" if val_acc >= 75.0 else "❌"
        f.write(f"| {epoch+1:5d} | {train_loss:.4f} | {train_acc:.2f}% | {val_loss:.4f} | {val_acc:.2f}% | {target_met} |\n")

def train_model(num_epochs=90, batch_size=256, learning_rate=0.1):
    # Store training parameters
    train_params = {
        'Epochs': num_epochs,
        'Batch Size': batch_size,
        'Initial Learning Rate': learning_rate,
        'Optimizer': 'SGD',
        'Momentum': 0.9,
        'Weight Decay': 1e-4,
        'LR Scheduler': 'CosineAnnealingLR'
    }
    
    # Create timestamp for logging
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Get S3 bucket info from environment
    bucket_name = os.getenv("S3_BUCKET_NAME", "era-2")  # Use era-2 as default
    
    # Data augmentation and normalization for training
    train_transform = transforms.Compose([
        transforms.RandomResizedCrop(224),
        transforms.RandomHorizontalFlip(),
        transforms.ColorJitter(brightness=0.4, contrast=0.4, saturation=0.4),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    ])
    
    val_transform = transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    ])
    
    # Load datasets directly from S3
    train_dataset = S3ImageNetDataset(bucket_name, transform=train_transform, is_train=True)
    val_dataset = S3ImageNetDataset(bucket_name, transform=val_transform, is_train=False)
    
    train_loader = DataLoader(
        train_dataset,
        batch_size=batch_size,
        shuffle=True,
        num_workers=4,
        pin_memory=True
    )
    
    val_loader = DataLoader(
        val_dataset,
        batch_size=batch_size,
        shuffle=False,
        num_workers=4,
        pin_memory=True
    )
    
    # Initialize model
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = ResNet50(num_classes=len(train_dataset.class_to_idx)).to(device)
    
    # Loss function and optimizer
    criterion = nn.CrossEntropyLoss(label_smoothing=0.1)
    optimizer = optim.SGD(
        model.parameters(),
        lr=learning_rate,
        momentum=0.9,
        weight_decay=1e-4
    )
    
    # Learning rate scheduler
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=num_epochs)
    
    # Training loop
    best_acc = 0
    target_acc_reached = False
    
    for epoch in range(num_epochs):
        # Training phase
        model.train()
        running_loss = 0.0
        correct = 0
        total = 0
        
        pbar = tqdm(train_loader, desc=f'Epoch {epoch + 1}/{num_epochs}')
        for inputs, labels in pbar:
            inputs, labels = inputs.to(device), labels.to(device)
            
            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs, labels)
            loss.backward()
            optimizer.step()
            
            running_loss += loss.item()
            _, predicted = outputs.max(1)
            total += labels.size(0)
            correct += predicted.eq(labels).sum().item()
            
            pbar.set_postfix({
                'loss': f'{running_loss/total:.4f}',
                'acc': f'{100.*correct/total:.2f}%'
            })
        
        train_acc = 100. * correct / total
        train_loss = running_loss / len(train_loader)
        
        # Validation phase
        model.eval()
        val_loss = 0
        correct = 0
        total = 0
        
        with torch.no_grad():
            for inputs, labels in tqdm(val_loader, desc='Validation'):
                inputs, labels = inputs.to(device), labels.to(device)
                outputs = model(inputs)
                loss = criterion(outputs, labels)
                
                val_loss += loss.item()
                _, predicted = outputs.max(1)
                total += labels.size(0)
                correct += predicted.eq(labels).sum().item()
        
        val_acc = 100. * correct / total
        val_loss = val_loss / len(val_loader)
        
        # Update learning rate
        scheduler.step()
        
        # Save training logs
        save_training_log(epoch, train_loss, train_acc, val_loss, val_acc, timestamp)
        
        # Log progress
        logging.info(
            f'Epoch [{epoch+1}/{num_epochs}] '
            f'Train Loss: {train_loss:.4f} Train Acc: {train_acc:.2f}% '
            f'Val Loss: {val_loss:.4f} Val Acc: {val_acc:.2f}%'
        )
        
        # Check if target accuracy is reached
        if val_acc >= 75.0 and not target_acc_reached:
            target_acc_reached = True
            logging.info(f'🎉 Target top-1 accuracy of 75% reached at epoch {epoch+1}!')
            torch.save(model.state_dict(), f'model_target_acc_{timestamp}.pth')
        
        # Save best model
        if val_acc > best_acc:
            best_acc = val_acc
            torch.save(model.state_dict(), f'best_model_{timestamp}.pth')
            logging.info(f'New best model saved with accuracy: {best_acc:.2f}%')
    
    # Final summary
    logging.info("\n" + "="*50)
    logging.info("Training Complete!")
    logging.info(f"Best Validation Accuracy: {best_acc:.2f}%")
    if target_acc_reached:
        logging.info("✅ Target top-1 accuracy of 75% was achieved!")
    else:
        logging.info("❌ Target top-1 accuracy of 75% was not reached.")
    logging.info(f"Training logs saved to: logs/training_log_{timestamp}.md")
    logging.info("="*50)

if __name__ == "__main__":
    train_model() 