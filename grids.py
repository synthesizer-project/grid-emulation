import h5py
from scipy.stats import qmc
import jax.numpy as jnp
import numpy as np

from synthesizer.grid import Grid
from synthesizer.particle import Stars
from synthesizer.emission_models import IncidentEmission
from unyt import Msun, yr, angstrom


# class SpectralDatasetJAX:
#     def __init__(
#         self,
#         h5_path='example_grid.hdf5',
#         spec_type='incident',
#         parent_dataset=None,
#         split=None,
#         ages='log10age',
#         metallicity='metallicity',
#     ):

#         if parent_dataset is not None:
#             self.spectra = parent_dataset.spectra
#             self.wavelength = parent_dataset.wavelength
#             self.ages = parent_dataset.ages
#             self.metallicities = parent_dataset.metallicities
#             self.conditions = parent_dataset.conditions
#             self.n_wavelength = parent_dataset.n_wavelength
#             self.n_age = parent_dataset.n_age
#             self.n_met = parent_dataset.n_met
#         else:
#             with h5py.File(h5_path, 'r') as f:
#                 self.spectra = jnp.array(f[f'spectra/{spec_type}'][:], dtype=jnp.float32)
#                 self.wavelength = jnp.array(f['spectra/wavelength'][:], dtype=jnp.float32)
#                 self.ages = jnp.array(f[f'axes/{ages}'][:], dtype=jnp.float32)
#                 self.metallicities = jnp.array(f[f'axes/{metallicity}'][:], dtype=jnp.float32)

#             # Filter spectra and wavelength
#             mask = (self.wavelength > 1000) & (self.wavelength < 10000)
#             self.spectra = self.spectra[:, :, mask]
#             self.wavelength = self.wavelength[mask]

#             # Get dimensions
#             self.n_age, self.n_met, self.n_wavelength = self.spectra.shape

#         if split is not None:
#             self.spectra = self.spectra[split]
#             self.ages = self.ages[split]
#             self.metallicities = self.metallicities[split]
#             self.conditions = self.conditions[split]
        
#         if parent_dataset is None:
#             # Normalize parameters
#             self.ages = (self.ages - self.ages.mean()) / self.ages.std()
#             self.metallicities = (self.metallicities - self.metallicities.mean()) / self.metallicities.std()
            
#             # Create all combinations of parameters
#             self.conditions = jnp.stack(jnp.meshgrid(self.ages, self.metallicities, indexing='ij')).reshape(2, -1).T

#             # Reshape spectra to match conditions
#             self.spectra = self.spectra.reshape(-1, self.n_wavelength)

#             # Log and normalize spectra
#             self.spectra = jnp.log10(self.spectra)
#             self.spectra = (self.spectra - self.spectra.mean(axis=1, keepdims=True)) / self.spectra.std(axis=1, keepdims=True)
    
#     def __len__(self):
#         return len(self.conditions)
    
#     def __getitem__(self, idx):
#         return self.conditions[idx], self.spectra[idx]
    

def LHGridSpectra(grid_dir, grid_name, num_samples=1000):
    grid = Grid(grid_dir=grid_dir, grid_name=grid_name, read_lines=False)

    N = num_samples
    age_lims = (np.log10(float(grid.ages.min().value)), np.log10(float(grid.ages.max().value)))
    met_lims = (float(grid.metallicities.min()), float(grid.metallicities.max()))

    print("Age limits: ", age_lims)
    print("Metallicity limits: ", met_lims)

    sampler = qmc.LatinHypercube(d=2)
    samples = qmc.scale(sampler.random(n=N), (age_lims[0], met_lims[0]), (age_lims[1], met_lims[1]))
    
    initial_masses = np.ones(N) * Msun

    stars = Stars(
        initial_masses=initial_masses,
        ages=10**samples[:, 0] * yr,
        metallicities=samples[:, 1],
    )

    emodel = IncidentEmission(grid, per_particle=True)
    spec = stars.get_spectra(emodel)

    mask = (grid.lam > 1000 * angstrom) & (grid.lam < 10000 * angstrom)
    spectra = spec.lnu[:, mask]
    wavelength = grid.lam[mask]

    ages = samples[:, 0]
    metallicities = samples[:, 1]

    return spectra, wavelength, ages, metallicities


class SpectralDatasetSynthesizer:
    def __init__(self, grid_dir=None, grid_name=None, num_samples=1000, parent_dataset=None, split=None):

        if parent_dataset is not None:
            # Inherit all data and parameters from the parent dataset
            self.spectra = parent_dataset.spectra
            self.wavelength = parent_dataset.wavelength
            self.ages = parent_dataset.ages
            self.metallicities = parent_dataset.metallicities
            self.conditions = parent_dataset.conditions
            self.n_wavelength = parent_dataset.n_wavelength
            
            # Carry over normalization parameters from the parent
            self.spec_mean = parent_dataset.spec_mean
            self.spec_std = parent_dataset.spec_std
            self.age_mean = parent_dataset.age_mean
            self.age_std = parent_dataset.age_std
            self.met_mean = parent_dataset.met_mean
            self.met_std = parent_dataset.met_std
            
        else:
            # Load raw data if this is a new dataset
            self.spectra, self.wavelength, self.ages, self.metallicities = LHGridSpectra(grid_dir, grid_name, num_samples)
            self.n_wavelength = self.spectra.shape[1]

            # --- Pre-computation before any splitting ---
            # Store normalization params for physical parameters
            self.age_mean, self.age_std = self.ages.mean(), self.ages.std()
            self.met_mean, self.met_std = self.metallicities.mean(), self.metallicities.std()

            # Normalize physical parameters
            norm_ages = (self.ages - self.age_mean) / self.age_std
            norm_mets = (self.metallicities - self.met_mean) / self.met_std
            
            # Create conditions from normalized parameters
            self.conditions = jnp.stack([norm_ages, norm_mets]).T

            # Reshape and log-transform spectra
            self.spectra = self.spectra.reshape(-1, self.n_wavelength)
            self.spectra = jnp.log10(self.spectra)

            # Calculate and store normalization parameters for spectra
            self.spec_mean = self.spectra.mean(axis=0)
            self.spec_std = self.spectra.std(axis=0)
            
            # Normalize spectra
            self.spectra = (self.spectra - self.spec_mean) / self.spec_std

        if split is not None:
            # Apply the split to all relevant arrays
            self.spectra = self.spectra[split]
            self.ages = self.ages[split]
            self.metallicities = self.metallicities[split]
            self.conditions = self.conditions[split]

    def unnormalize_spectrum(self, spectrum):
        """Un-normalizes a single spectrum using the stored dataset parameters."""
        # return 10**((spectrum * self.spec_std) + self.spec_mean)
        return (spectrum * self.spec_std) + self.spec_mean

    def unnormalize_age(self, norm_age):
        """Un-normalizes a single age value."""
        return (norm_age * self.age_std) + self.age_mean

    def unnormalize_metallicity(self, norm_met):
        """Un-normalizes a single metallicity value."""
        return (norm_met * self.met_std) + self.met_mean
    
    def __len__(self):
        return len(self.conditions)
    
    def __getitem__(self, idx):
        return self.conditions[idx], self.spectra[idx]


if __name__ == '__main__':
    spectra, conditions, wavelength, ages, metallicities = LHGridSpectra()

    print(spectra.shape)
    print(conditions.shape)
    print(wavelength.shape)
    print(ages.shape)
    print(metallicities.shape)