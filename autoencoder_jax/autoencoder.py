import sys
sys.path.append('..')

import numpy as np
import matplotlib.pyplot as plt
import jax
import jax.numpy as jnp
import flax.linen as nn
import optax
from typing import Sequence
from flax.training import train_state
from flax import serialization

from grids import SpectralDatasetSynthesizer


# === Model Architecture ===
class SpectrumEncoder(nn.Module):
    features: Sequence[int] = (1024, 512, 256)
    latent_dim: int = 64
    
    @nn.compact
    def __call__(self, x, training: bool = True):
        for feat in self.features:
            x = nn.Dense(feat, kernel_init=nn.initializers.he_normal())(x)
            x = nn.BatchNorm(use_running_average=not training, momentum=0.9)(x)
            x = nn.relu(x)
            if training:
                x = nn.Dropout(rate=0.2, deterministic=not training)(x)
        x = nn.Dense(self.latent_dim, kernel_init=nn.initializers.he_normal())(x)
        return x

class SpectrumDecoder(nn.Module):
    features: Sequence[int] = (256, 512, 1024)
    spectrum_dim: int = 10787
    
    @nn.compact
    def __call__(self, x, training: bool = True):
        for feat in self.features:
            x = nn.Dense(feat, kernel_init=nn.initializers.he_normal())(x)
            x = nn.BatchNorm(use_running_average=not training, momentum=0.9)(x)
            x = nn.relu(x)
            if training:
                x = nn.Dropout(rate=0.2, deterministic=not training)(x)
        x = nn.Dense(self.spectrum_dim, kernel_init=nn.initializers.he_normal())(x)
        return x

class SpectrumAutoencoder(nn.Module):
    spectrum_dim: int
    latent_dim: int
    
    def setup(self):
        self.encoder = SpectrumEncoder(latent_dim=self.latent_dim)
        self.decoder = SpectrumDecoder(spectrum_dim=self.spectrum_dim)
    
    def encode(self, spectrum, training: bool = True):
        return self.encoder(spectrum, training=training)
    
    def decode(self, latent, training: bool = True):
        return self.decoder(latent, training=training)
    
    def __call__(self, spectrum, training: bool = True):
        latent = self.encode(spectrum, training=training)
        return self.decode(latent, training=training)

# === Training State ===
class TrainState(train_state.TrainState):
    batch_stats: dict

    @classmethod
    def create(cls, *, apply_fn, params, batch_stats, tx):
        return cls(
            step=0,
            apply_fn=apply_fn,
            params=params,
            batch_stats=batch_stats,
            tx=tx,
            opt_state=tx.init(params) if tx is not None else None,
        )

# === Training Functions ===
def create_train_state(rng, model, learning_rate):
    """Creates initial training state."""
    rng, init_rng = jax.random.split(rng)
    variables = model.init(init_rng, jnp.ones((1, model.spectrum_dim)))
    params = variables['params']
    batch_stats = variables['batch_stats']
    
    # Optimized optimizer configuration
    tx = optax.chain(
        optax.clip_by_global_norm(1.0),
        optax.adamw(
            learning_rate=learning_rate,
            weight_decay=1e-4,
            b1=0.9,
            b2=0.999,
            eps=1e-8
        )
    )
    
    return TrainState.create(
        apply_fn=model.apply,
        params=params,
        batch_stats=batch_stats,
        tx=tx
    )

@jax.jit
def train_step(state, batch, rng):
    """Performs a single training step."""
    def loss_fn(params, rng):
        spectrum = batch
        variables = {'params': params, 'batch_stats': state.batch_stats}
        rng, dropout_rng = jax.random.split(rng)
        pred_spectrum, new_model_state = state.apply_fn(
            variables, spectrum,
            mutable=['batch_stats'],
            training=True,
            rngs={'dropout': dropout_rng}
        )
        # Add L2 regularization
        l2_loss = 0.5 * sum(jnp.sum(jnp.square(p)) for p in jax.tree_util.tree_leaves(params))
        reconstruction_loss = jnp.mean((pred_spectrum - spectrum) ** 2)
        loss = reconstruction_loss + 1e-4 * l2_loss
        return loss, (new_model_state, reconstruction_loss, l2_loss)  # Return components
    
    grad_fn = jax.value_and_grad(loss_fn, has_aux=True)
    (loss, (new_model_state, recon_loss, l2_loss)), grads = grad_fn(state.params, rng)
    state = state.apply_gradients(
        grads=grads,
        batch_stats=new_model_state['batch_stats']
    )
    return state, loss, recon_loss, l2_loss

@jax.jit
def eval_step(state, batch, training=False):
    """Performs a single evaluation step."""
    spectrum = batch
    variables = {'params': state.params, 'batch_stats': state.batch_stats}
    pred_spectrum = state.apply_fn(
        variables, spectrum,
        training=training  # Allow evaluation in training mode
    )
    loss = jnp.mean((pred_spectrum - spectrum) ** 2)
    return loss

def train_epoch(state, train_ds, batch_size, rng):
    """Trains for a single epoch."""
    train_ds_size = len(train_ds)
    steps_per_epoch = train_ds_size // batch_size
    
    perms = jax.random.permutation(rng, train_ds_size)
    perms = perms[:steps_per_epoch * batch_size]
    perms = perms.reshape((steps_per_epoch, batch_size))
    
    epoch_loss = []
    epoch_recon_loss = []
    epoch_l2_loss = []
    
    for perm in perms:
        batch = train_ds.spectra[perm]
        rng, step_rng = jax.random.split(rng)
        state, loss, recon_loss, l2_loss = train_step(state, batch, step_rng)
        epoch_loss.append(loss)
        epoch_recon_loss.append(recon_loss)
        epoch_l2_loss.append(l2_loss)
    
    return state, {
        'total_loss': np.array(jnp.mean(jnp.array(epoch_loss))),
        'recon_loss': np.array(jnp.mean(jnp.array(epoch_recon_loss))),
        'l2_loss': np.array(jnp.mean(jnp.array(epoch_l2_loss)))
    }, rng

def eval_model(state, test_ds, batch_size):
    """Evaluates the model on the test set."""
    test_ds_size = len(test_ds)
    steps_per_epoch = test_ds_size // batch_size
    
    all_losses = []
    
    for i in range(steps_per_epoch):
        batch = test_ds.spectra[i * batch_size:(i + 1) * batch_size]
        loss = eval_step(state, batch)
        all_losses.append(loss)
    
    # Convert to numpy only at the end for plotting
    return np.array(jnp.mean(jnp.array(all_losses)))

def train_and_evaluate(
    model,
    train_ds,
    test_ds,
    num_epochs,
    batch_size,
    learning_rate,
    rng,
    patience=10,
    min_delta=1e-4,
    lr_patience=3,
    lr_factor=0.5,
    min_lr=1e-7
):
    """Trains the model and evaluates it."""
    rng, init_rng = jax.random.split(rng)
    state = create_train_state(init_rng, model, learning_rate)
    
    train_losses = []
    test_losses = []
    best_test_loss = float('inf')
    best_epoch = 0
    no_improve_epochs = 0
    
    # Learning rate reduction tracking
    current_lr = learning_rate
    lr_no_improve_epochs = 0
    
    for epoch in range(num_epochs):
        rng, input_rng = jax.random.split(rng)
        state, train_losses_epoch, rng = train_epoch(state, train_ds, batch_size, input_rng)
        test_loss = eval_model(state, test_ds, batch_size)
        
        train_losses.append(train_losses_epoch['total_loss'])
        test_losses.append(test_loss)
        
        print(f"Epoch {epoch + 1}/{num_epochs}")
        print(f"Train Loss: {train_losses_epoch['total_loss']:.4f}, Test Loss: {test_loss:.4f}")
        print(f"Learning Rate: {current_lr:.2e}")
        
        # Early stopping check
        if test_loss < best_test_loss - min_delta:
            best_test_loss = test_loss
            best_epoch = epoch
            no_improve_epochs = 0
            lr_no_improve_epochs = 0
            # Save best model using flax serialization
            state_dict = {
                'params': state.params,
                'batch_stats': state.batch_stats,
                'step': state.step
            }
            with open('models/best_autoencoder.msgpack', 'wb') as f:
                f.write(serialization.to_bytes(state_dict))
        else:
            no_improve_epochs += 1
            lr_no_improve_epochs += 1
            print(f"No improvement for {no_improve_epochs} epochs")
            
            # Learning rate reduction
            if lr_no_improve_epochs >= lr_patience:
                new_lr = max(current_lr * lr_factor, min_lr)
                if new_lr != current_lr:
                    print(f"Reducing learning rate from {current_lr:.2e} to {new_lr:.2e}")
                    current_lr = new_lr
                    lr_no_improve_epochs = 0
                    # Update optimizer with new learning rate
                    tx = optax.chain(
                        optax.clip_by_global_norm(1.0),
                        optax.adamw(
                            learning_rate=current_lr,
                            weight_decay=1e-4,
                            b1=0.9,
                            b2=0.999,
                            eps=1e-8
                        )
                    )
                    state = state.replace(tx=tx)
        
        # Early stopping
        if no_improve_epochs >= patience:
            print(f"\nEarly stopping triggered after {epoch + 1} epochs")
            print(f"Best test loss: {best_test_loss:.4f} at epoch {best_epoch + 1}")
            break
    
    return state, train_losses, test_losses

def main():
    # Check for available devices
    print(f"JAX devices: {jax.devices()}")
    
    # Load dataset
    N_samples = int(1e3)
    spec_type='stellar'  # 'incident'
    grid_dir = '/home/chris/code/synthesizer_grids/grids/'
    # grid_dir = '../../synthesizer_data/grids/'
    dataset = SpectralDatasetSynthesizer(grid_dir=grid_dir, grid_name='bc03-2016-Miles_chabrier-0.1,100.hdf5', num_samples=N_samples)
    # dataset = SpectralDatasetJAX(f'{grid_dir}/bc03_chabrier03-0.1,100.hdf5', spec_type=spec_type)
    
    # Split dataset
    rng = jax.random.PRNGKey(0)
    rng, split_rng = jax.random.split(rng)
    perm = jax.random.permutation(split_rng, len(dataset))
    split = int(0.8 * len(dataset))
    
    train_dataset = SpectralDatasetSynthesizer(parent_dataset=dataset, split=perm[:split])
    test_dataset = SpectralDatasetSynthesizer(parent_dataset=dataset, split=perm[split:])
    
    # Create model
    model = SpectrumAutoencoder(
        spectrum_dim=train_dataset.n_wavelength,
        latent_dim=128  # This is a hyperparameter we choose
    )
    
    # Training parameters
    num_epochs = 100
    batch_size = 32
    learning_rate = 1e-3
    
    # Train model
    state, train_losses, test_losses = train_and_evaluate(
        model=model,
        train_ds=train_dataset,
        test_ds=test_dataset,
        num_epochs=num_epochs,
        batch_size=batch_size,
        learning_rate=learning_rate,
        rng=rng
    )
    
    # Plot training curves
    plt.figure(figsize=(10, 4))
    plt.plot(train_losses, label='Train Loss')
    plt.plot(test_losses, label='Test Loss')
    plt.xlabel('Epoch')
    plt.ylabel('Loss')
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.savefig('figures/autoencoder_training_curves.png', dpi=300, bbox_inches='tight')
    plt.close()

if __name__ == "__main__":
    main() 
