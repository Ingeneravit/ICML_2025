import os
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
import torch.nn.functional as F
from torchvision import datasets, transforms, utils
from skimage.metrics import structural_similarity as ssim
import matplotlib.pyplot as plt
from PIL import Image
import numpy as np

# Set device
device = 'mps'

# Hyperparameters 
batch_size = 64
lr = 0.0002 # original 0.0002
latent_dim = 100
image_size = 28 * 28
epochs = 50
images_per_grid = 80 * 45  # 80 x 45 images per grid

# Create folder to save generated images
os.makedirs("gen_images", exist_ok=True)

# Open a file to log losses
loss_log_file = "gen_images/loss_log.txt"
with open(loss_log_file, "w") as f:
    f.write("epoch, batch, d_loss, g_loss, RMS_avg, Edge_intensity_avg, SSIM_avg, Contrast_avg, Entropy_avg\n")  # Header for readability

# Data loader
transform = transforms.Compose([
    transforms.ToTensor(),
    transforms.Normalize([0.5], [0.5])  # Normalize to [-1, 1] for Tanh activation
])

train_data = datasets.MNIST(root='./data', train=True, download=True, transform=transform)
train_loader = DataLoader(train_data, batch_size=batch_size, shuffle=True)


# Function to set uniform activation for the generator input
def set_generator_activation(generator, mode='random', value=0.5):
    def input_activation(batch_size, latent_dim, device):
        if mode == 'random':
            return torch.randn(batch_size, latent_dim, device=device)
        elif mode == 'uniform':
            return torch.full((batch_size, latent_dim), fill_value=value, device=device)
        else:
            raise ValueError("Mode must be 'random' or 'uniform'.")

    generator.input_activation = input_activation


# Generator
class Generator(nn.Module):
    def __init__(self, input_dim, output_dim):
        super(Generator, self).__init__()
        self.model = nn.Sequential(
            nn.Linear(input_dim, 256),
            nn.LeakyReLU(0.2),
            nn.Linear(256, 512),
            nn.LeakyReLU(0.2),
            nn.Linear(512, 1024),
            nn.LeakyReLU(0.2),
            nn.Linear(1024, output_dim),
            nn.Tanh()
        )
        # Default input activation uses random noise
        self.input_activation = lambda batch_size, latent_dim, device: torch.randn(batch_size, latent_dim, device=device)

    def forward(self, x):
        return self.model(x)

# Discriminator
class Discriminator(nn.Module):
    def __init__(self, input_dim):
        super(Discriminator, self).__init__()
        self.model = nn.Sequential(
            nn.Linear(input_dim, 512),
            nn.LeakyReLU(0.2),
            nn.Linear(512, 256),
            nn.LeakyReLU(0.2),
            nn.Linear(256, 1),
            nn.Sigmoid()
        )

    def forward(self, x):
        return self.model(x)

# Initialize models
generator = Generator(latent_dim, image_size).to(device)
discriminator = Discriminator(image_size).to(device)

# Set generator to use uniform activation with value 0.5
set_generator_activation(generator, mode='uniform', value=0.5)

# Loss and optimizers
criterion = nn.BCELoss()
optimizer_G = optim.Adam(generator.parameters(), lr=lr, betas=(0.5, 0.999))
optimizer_D = optim.Adam(discriminator.parameters(), lr=lr, betas=(0.5, 0.999))

# Function to calculate SNR
def calculate_snr(image):
    signal_power = torch.mean(image ** 2).item()  # Signal power (mean squared value)
    noise_power = torch.var(image).item()        # Noise power (variance)
    if noise_power == 0:
        return float('inf')  # Avoid division by zero (perfect signal)
    return 10 * np.log10(signal_power / noise_power)

# Function to calculate Sobel-based edge intensity
def calculate_edge_intensity(image):
    sobel_kernel_x = torch.tensor([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], dtype=torch.float32, device=device).unsqueeze(0).unsqueeze(0)
    sobel_kernel_y = torch.tensor([[-1, -2, -1], [0, 0, 0], [1, 2, 1]], dtype=torch.float32, device=device).unsqueeze(0).unsqueeze(0)

    if len(image.shape) == 3:
        image = image.unsqueeze(0)  # Add a batch dimension
    elif len(image.shape) == 2:
        image = image.unsqueeze(0).unsqueeze(0)  # Add batch and channel dimensions

    Gx = F.conv2d(image, sobel_kernel_x, padding=1)  # Gradient along x-axis
    Gy = F.conv2d(image, sobel_kernel_y, padding=1)  # Gradient along y-axis

    edge_intensity = torch.sqrt(Gx ** 2 + Gy ** 2).mean().item()  # Mean edge intensity
    return edge_intensity

# Function to calculate SSIM between two images
def calculate_ssim(img1, img2):
    img1 = img1.cpu().numpy().squeeze()
    img2 = img2.cpu().numpy().squeeze()
    return ssim(img1, img2, data_range=img2.max() - img2.min())


# Function to calculate normalized contrast
def calculate_normalized_contrast(image):
    image = image.squeeze()  # Remove unnecessary dimensions
    mean_intensity = image.mean().item()
    std_deviation = image.std().item()

    # Avoid division by zero by adding a small epsilon
    epsilon = 1e-8
    normalized_contrast = std_deviation / (mean_intensity + epsilon)
    return normalized_contrast

# Function to calculate entropy
def calculate_entropy(image):
    image = image.squeeze().cpu().numpy()  # Convert to numpy array
    image = (image - image.min()) / (image.max() - image.min() + 1e-8)  # Normalize to [0, 1]

    histogram, bin_edges = np.histogram(image, bins=256, range=(0, 1), density=True)
    histogram = histogram + 1e-8  # Avoid log(0) by adding small value
    entropy = -np.sum(histogram * np.log2(histogram))
    return entropy



# Training
for epoch in range(epochs):
    if epoch < 2:
        save_interval = 10
    elif 2 <= epoch < 5:
        save_interval = 30
    elif 5 <= epoch < 10:
        save_interval = 50
    elif 10 <= epoch < 30:
        save_interval = 100
    else:
        save_interval = 200
    
    for i, (imgs, _) in enumerate(train_loader):
        real = torch.ones(imgs.size(0), 1).to(device)
        fake = torch.zeros(imgs.size(0), 1).to(device)

        imgs = imgs.view(imgs.size(0), -1).to(device)

        # Train Discriminator
        optimizer_D.zero_grad()
        real_loss = criterion(discriminator(imgs), real)
        z = generator.input_activation(batch_size, latent_dim, device)
        generated_imgs = generator(z)
        fake_loss = criterion(discriminator(generated_imgs.detach()), fake)
        d_loss = real_loss + fake_loss
        d_loss.backward()
        optimizer_D.step()

        # Train Generator
        optimizer_G.zero_grad()
        g_loss = criterion(discriminator(generated_imgs), real)
        g_loss.backward()
        optimizer_G.step()

        if i % 200 == 0:
            print(f"Epoch [{epoch}/{epochs}] Batch [{i}/{len(train_loader)}] Loss D: {d_loss.item():.4f}, Loss G: {g_loss.item():.4f}")


        if i % save_interval == 0:
            with open(loss_log_file, "a") as f:
                with torch.no_grad():
                    z = torch.randn(100, latent_dim).to(device)
                    random_generated_imgs = generator(z).view(-1, 1, 28, 28)

                    # Edge Intensity
                    edge_intensities = [calculate_edge_intensity(img) for img in random_generated_imgs]
                    edge_intensity_avg = sum(edge_intensities) / len(edge_intensities)

                    # RMS
                    rms_values = [torch.sqrt(torch.mean(img ** 2)).item() for img in random_generated_imgs]
                    rms_avg = sum(rms_values) / len(rms_values)

                    # SSIM
                    ssim_values = []
                    for j in range(0, len(random_generated_imgs), 2):
                        if j + 1 < len(random_generated_imgs):
                            ssim_values.append(calculate_ssim(random_generated_imgs[j], random_generated_imgs[j + 1]))
                    ssim_avg = sum(ssim_values) / len(ssim_values)

                    # Normalized Contrast
                    contrast_values = [calculate_normalized_contrast(img) for img in random_generated_imgs]
                    contrast_avg = sum(contrast_values) / len(contrast_values)

                    # Entropy
                    entropy_values = [calculate_entropy(img) for img in random_generated_imgs]
                    entropy_avg = sum(entropy_values) / len(entropy_values)

                # Log all metrics
                f.write(f"{epoch}, {i}, {d_loss.item():.4f}, {g_loss.item():.4f}, {rms_avg:.4f}, {edge_intensity_avg:.4f}, {ssim_avg:.4f}, {contrast_avg:.4f}, {entropy_avg:.4f}\n")


            with torch.no_grad():
                z = generator.input_activation(batch_size, latent_dim, device)
                generated_imgs = generator(z).view(-1, 1, 28, 28)

                grid = utils.make_grid(generated_imgs, nrow=80, normalize=True)
                grid_np = (grid.permute(1, 2, 0).cpu().numpy() * 255).astype(np.uint8)
                image = Image.fromarray(grid_np).resize((1920, 1080), Image.LANCZOS)

                grid_filename = f"gen_images/generated_epoch_{epoch}_batch_{i}.png"
                image.save(grid_filename)
                print(f"Saved generated images to {grid_filename} with size {image.size[0]}x{image.size[1]}")
