from argparse import ArgumentParser
import scipy
import numpy
import sys


# Manage command line arguments
parser = ArgumentParser()

parser.add_argument('--device', default='mps', choices=['cpu', 'cuda', 'mps'], type=str,
                    help='Set device to be utilized. cuda or cpu.')
parser.add_argument('--epochs', default=10000, type=int,
                    help='Training epochs to be performed.')
parser.add_argument('--d_updates', default=1, type=int,
                    help='Discriminator updates per generator update.')
parser.add_argument('--plot_frequency', default=10, type=int,
                    help='Frequency of epochs to produce plots.')
parser.add_argument('--lr', default=0.0001, type=float,
                    help='Learning rate to be applied.')
parser.add_argument('--latent_size', default=32, type=int,
                    help='Size of latent vector to be utilized.')
parser.add_argument('--samples', default=10000, type=int,
                    help='Number of samples from the real distribution.')
parser.add_argument('--batch_size', default=500, type=int,
                    help='Batch size to be utilized.')
parser.add_argument('--loss', default='standard', type=str,
                    choices=['standard', 'non-saturating', 'hinge', 'wasserstein', 'wasserstein-gp', 'least-squares'],
                    help='GAN loss function to be used.')
parser.add_argument('--spectral_norm', default=False, action='store_true',
                    help='If set use spectral norm to stabilize discriminator.')
parser.add_argument('--clip_weights', default=0., type=float,
                    help='If > 0., weights will be clipped to [-clip_weights, clip_weights].')
parser.add_argument('--topk', default=False, action='store_true',
                    help='If set top-k training is utilized after 0.5 of the epochs to be performed.')
parser.add_argument('--gen_picture', default=False, type=bool,
                    help='If set, the generator will produce a picture.')

# Get arguments
args = parser.parse_args() 

# args.device = 'cpu'
# args.epochs = 10000
# args.d_updates = 1
# args.plot_frequency = 10
args.lr = 0.00004
# args.latent_size = 32
# args.samples = 10000
# args.batch_size = 500
# args.loss = 'non-saturating'
# args.spectral_norm = False
# args.clip_weights = 0.
# args.topk = False
args.gen_picture = True

print(args)

import torch
import torch.nn as nn
from tqdm import tqdm
import matplotlib.pyplot as plt
import os

import utils
import loss

from pythonosc import udp_client
client = udp_client.SimpleUDPClient('127.0.0.1', 7777)


# Clear output file
with open("output.txt", "w") as file:
    file.truncate(0)


if __name__ == '__main__':
    # Make directory to save plots
    path = os.path.join(os.getcwd(), 'plots')
    os.makedirs(path, exist_ok=True)
    # Init hyperparameters
    fixed_generator_noise: torch.Tensor = torch.randn([args.samples // 10, args.latent_size], device=args.device)
    # Get data
    data: torch.Tensor = utils.get_data(samples=args.samples).to(args.device)
    # Get generator
    generator: nn.Module = utils.get_generator(latent_size=args.latent_size)
    # Get discriminator
    discriminator: nn.Module = utils.get_discriminator(use_spectral_norm=args.spectral_norm)
    # Init Loss function
    if args.loss == 'standard':
        loss_generator: nn.Module = loss.GANLossGenerator()
        loss_discriminator: nn.Module = loss.GANLossDiscriminator()
    elif args.loss == 'non-saturating':
        loss_generator: nn.Module = loss.NSGANLossGenerator()
        loss_discriminator: nn.Module = loss.NSGANLossDiscriminator()
    elif args.loss == 'hinge':
        loss_generator: nn.Module = loss.HingeGANLossGenerator()
        loss_discriminator: nn.Module = loss.HingeGANLossDiscriminator()
    elif args.loss == 'wasserstein':
        loss_generator: nn.Module = loss.WassersteinGANLossGenerator()
        loss_discriminator: nn.Module = loss.WassersteinGANLossDiscriminator()
    elif args.loss == 'wasserstein-gp':
        loss_generator: nn.Module = loss.WassersteinGANLossGPGenerator()
        loss_discriminator: nn.Module = loss.WassersteinGANLossGPDiscriminator()
    else:
        loss_generator: nn.Module = loss.LSGANLossGenerator()
        loss_discriminator: nn.Module = loss.LSGANLossDiscriminator()
    # Networks to train mode
    generator.train()
    discriminator.train()
    # Models to device
    generator.to(args.device)
    discriminator.to(args.device)
    # Init optimizer
    generator_optimizer: torch.optim.Optimizer = torch.optim.RMSprop(generator.parameters(), lr=args.lr)
    discriminator_optimizer: torch.optim.Optimizer = torch.optim.RMSprop(discriminator.parameters(), lr=args.lr)
    # Init progress bar
    progress_bar = tqdm(total=args.epochs)
    # Training loop
    for epoch in range(args.epochs):
        # Update progress bar
        progress_bar.update(n=1)
        for index in range(0, args.samples, args.batch_size):
            # Shuffle data
            data = data[torch.randperm(data.shape[0], device=args.device)]
            # Update discriminator more often than generator to train it till optimality and get more reliable gradients of Wasserstein
            for _ in range(args.d_updates):
                # Get batch
                batch: torch.Tensor = data[index:index + args.batch_size]
                # Get noise for generator
                noise: torch.Tensor = torch.randn([args.batch_size, args.latent_size], device=args.device)
                # Optimize discriminator
                discriminator_optimizer.zero_grad()
                generator_optimizer.zero_grad()
                with torch.no_grad():
                    fake_samples: torch.Tensor = generator(noise)
                prediction_real: torch.Tensor = discriminator(batch)
                prediction_fake: torch.Tensor = discriminator(fake_samples)
                if isinstance(loss_discriminator, loss.WassersteinGANLossGPDiscriminator):
                    loss_d: torch.Tensor = loss_discriminator(prediction_real, prediction_fake, discriminator, batch,
                                                            fake_samples)
                else:
                    loss_d: torch.Tensor = loss_discriminator(prediction_real, prediction_fake)
                loss_d.backward()
                discriminator_optimizer.step()

                # Clip weights to enforce Lipschitz constraint as proposed in Wasserstein GAN paper
                if args.clip_weights > 0:
                    with torch.no_grad():
                        for param in discriminator.parameters():
                            param.clamp_(-args.clip_weights, args.clip_weights)

            # Get noise for generator
            noise: torch.Tensor = torch.randn([args.batch_size, args.latent_size], device=args.device)
            # Optimize generator
            discriminator_optimizer.zero_grad()
            generator_optimizer.zero_grad()
            fake_samples: torch.Tensor = generator(noise)
            prediction_fake: torch.Tensor = discriminator(fake_samples)
            if args.topk and (epoch >= 0.5 * args.epochs):
                prediction_fake = torch.topk(input=prediction_fake[:, 0], k=prediction_fake.shape[0] // 2)[0]
            loss_g: torch.Tensor = loss_generator(prediction_fake)
            loss_g.backward()
            generator_optimizer.step()
            # Update progress bar description
            progress_bar.set_description(
                'Epoch {}, Generator loss {:.4f}, Discriminator loss {:.4f}'.format(epoch, loss_g.item(),
                                                                                    loss_d.item()))
        # Plot samples of generator
        if ((epoch + 1) % args.plot_frequency) == 0:
            generator.eval()
            generator_samples = generator(fixed_generator_noise)
            generator_samples = generator_samples.cpu().detach().numpy()
            
            plt.figure(figsize=(13, 13))

            plt.scatter(data[::10, 0].cpu(), data[::10, 1].cpu(), color='black', s=2, alpha=0.5)
            #label='Samples from $p_{data}$'
            plt.scatter(generator_samples[:, 0], generator_samples[:, 1], color='orange',
                        s=2, alpha=0.5)
            # label='Samples from generator $G$' 

            # plt.legend(loc=1)
            # plt.title('Step {}'.format((epoch + 1) * args.samples // args.batch_size))

            # Calculate the center and radius for each group of points
            angles = torch.tensor([numpy.pi/8, 3*numpy.pi/8, 5*numpy.pi/8, 7*numpy.pi/8, 9*numpy.pi/8, 11*numpy.pi/8, 13*numpy.pi/8, 15*numpy.pi/8])
            centers = torch.stack([torch.cos(angles), torch.sin(angles)], dim=1)
            # radius = torch.sqrt(torch.sum((data.unsqueeze(1) - centers.unsqueeze(0))**2, dim=2)).max(dim=0).values
            radius = torch.tensor([0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5])

            def plot_data_with_circles(data: torch.Tensor):
                
                # Plot circles around each group of points
                colors = ['black', 'navy', 'blue', 'purple', 'violet', 'hotpink', 'crimson', 'maroon']
                i = 7

                for center, r in zip(centers, radius):
                    i = (i + 1) % 8
                    circle = plt.Circle(center, r, color='white', fill=False)
                    plt.gca().add_patch(circle)

            def get_point_loc():
                generator_samples_tensor = torch.from_numpy(generator_samples)
                sums = [0] * len(centers)
                for i, center in enumerate(centers):
                    for j, point in enumerate(generator_samples_tensor):
                        if ((point[0].to(args.device) - center[0].to(args.device))**2 + (point[1].to(args.device) - center[1].to(args.device))**2) < radius[0]**2:
                            sums[i] += 1                            

                print(sums)
                
                with open('output.txt', 'a') as file:
                    file.write(' '.join(map(str, sums)) + '\n')

                client.send_message('/points', sums)


            plot_data_with_circles(data)                
            get_point_loc()
            

            
            plt.xlim((-1.7, 1.7))
            plt.ylim((-1.7, 1.7))
            plt.grid()
            plt.axis("off")

            plt.savefig(os.path.join(path, '{}.png'.format(str(epoch + 1).zfill(4))), pad_inches=0)
            plt.close()
            
            
            numpy.set_printoptions(threshold=sys.maxsize)
            # print(data[::10, 0].cpu())

            # divergence = scipy.special.kl_div([data[::10, 0].cpu(), data[::10, 1].cpu()], [generator_samples[:, 0], generator_samples[:, 1]])
            # divergence_sum = numpy.sum(divergence)
            # print('KL Divergence: {}'.format(divergence_sum))

            generator.train()



# for circle with center (center_x, center_y) and radius radius; point (x, y) is inside the circle if:
# (x - center_x)² + (y - center_y)² < radius²