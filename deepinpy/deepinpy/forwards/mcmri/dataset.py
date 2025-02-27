#!/usr/bin/env python
"""A database object for MCMRI data that enables compatability with PyTorch."""

import torch.utils.data
import numpy as np
import scipy.fftpack
import h5py
import pathlib

import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

from deepinpy.utils.utils import fftmod, fftshift, fft2uc, ifft2uc

class MultiChannelMRIDataset(torch.utils.data.Dataset):
    """A Multichannel MRI Dataset, compatible with PyTorch.

    This class is designed to be used in the System block of the Recon object as a data holder;
    it is passed into other sub-blocks for preprocessing before entering the model and optimization blocks.

    Args:
        data_file (string, path): Path to the file the data is stored in.
        stdev (float): Standard deviation.
        num_data_sets (int): Restrict to this many data samples
        adjoint_data (bool): Apply adjoint operation before returning input
        preload (bool): …
        id (int): …
        clear_cache (bool): …
        cache_data (bool): …
        gen_masks (bool): ...
        scale_data (bool): …
        fully_sampled (bool): Use a fully sampled mask
        data_idx (bool): …
        inverse_crime (bool): Generate the data from GT image
        noncart (bool): Non-Cartesian (experimental)

    """

    def __init__(self, data_file, masks_file, stdev=.01, num_data_sets=None, adjoint_data=True, preload=False, id=None, clear_cache=False, cache_data=False, gen_masks=False, scale_data=False, fully_sampled=False, data_idx=None, inverse_crime=False, noncart=False):

        self.data_file = data_file
        self.masks_file = masks_file
        self.stdev = stdev
        self.adjoint_data = adjoint_data
        self.preload = preload
        self.id = id
        self.cache_data = cache_data
        self.gen_masks = gen_masks
        self.max_data_sets = self._len()
        self.fully_sampled = fully_sampled
        self.data_idx = data_idx
        self.scale_data = scale_data
        self.inverse_crime = inverse_crime
        self.noncart = noncart

        if self.data_idx is not None:
            self.num_data_sets = 1
        else:
            if num_data_sets is None:
                self.num_data_sets = self.max_data_sets
            else:
                self.num_data_sets = int(np.minimum(num_data_sets, self.max_data_sets))

        if self.cache_data and clear_cache:
            print('clearing cache')
            for p in pathlib.Path('.').glob('cache/{}_*_{}'.format(self.id, self.data_file)):
                pathlib.Path.unlink(p)

    def _len(self):
        with h5py.File(self.data_file, 'r') as F:
            max_data_sets = F['imgs'].shape[0]
            self.shape = F['imgs'].shape
        return max_data_sets

    def __len__(self):
        return self.num_data_sets
    
    def shape(self):
        return self.shape

    def __getitem__(self, idx):

        if self.data_idx is not None:
            idx = self.data_idx

        # Caching is never used in our code.
        # if self.cache_data and self.id: 
        #     data_file = 'cache/{}_{}_{}'.format(self.id, idx, self.data_file)
        #     try:
        #         imgs, maps, masks, out, loss_masks = load_data_cached(data_file)
        #     except:
        #         imgs, maps, masks, out, loss_masks = self._load_data(idx)
        #         save_data_cached(data_file, imgs, maps, masks, out, loss_masks)
        # else:
        imgs, maps, masks, out, loss_masks = self._load_data(idx)

        if imgs.shape[0] == 1:
            imgs = imgs.squeeze(0)
            maps = maps.squeeze(0)
            masks = masks.squeeze(0)
            out = out.squeeze(0)
            loss_masks = loss_masks.squeeze(0)

        data = {
                'imgs': imgs.astype(np.complex64),
                'maps': maps.astype(np.complex64),
                'masks': masks.astype(np.float32),
                'loss_masks': loss_masks.astype(np.float32),
                'out': out.astype(np.complex64)
                }

        return idx, data

    def _load_data(self, idx):
        if self.inverse_crime:
            #imgs, maps, masks = load_data_legacy(idx, self.data_file, self.gen_masks)
            imgs, maps, masks, noise, loss_masks = load_data(idx, self.data_file, self.masks_file, self.gen_masks)
        else:
            imgs, maps, masks, ksp, noise, loss_masks = load_data_ksp(idx, self.data_file, self.masks_file, self.gen_masks)
        if self.scale_data:
            ## FIXME: batch mode
            assert not self.scale_data, 'SEE FIXME'
            sc = np.percentile(abs(imgs), 99, axis=(-1, -2))
            imgs = imgs / sc
            ksp = ksp / sc

        if self.fully_sampled:
            masks = np.ones(masks.shape)

        if self.inverse_crime:
            assert not self.noncart, 'FIXME: forward sim of NUFFT'
            out = self._sim_data(imgs, maps, masks, noise)
        else:
            out = self._sim_data(imgs, maps, masks, noise, ksp)

        if not self.noncart:
            maps = fftmod(maps)
        return imgs, maps, masks, out, loss_masks

    def _sim_data(self, imgs, maps, masks, noise, ksp=None):
        if self.noncart:
            from mrinufft import get_operator
            fourier_op = get_operator("gpunufft")(masks[0], maps.shape[-2:], n_coils=maps.shape[1])
            per_ch_img = ifft2uc(ksp)
            out = fourier_op.op(per_ch_img)
            if out.ndim == 2:
                out = out[None]
            if noise is None:
                noise = np.random.randn(*out.shape) + 1j * np.random.randn(*out.shape)
        else:
            # N, nc, nx, ny
            if noise is None:
                noise = np.random.randn(*maps.shape) + 1j * np.random.randn(*maps.shape)

            if self.inverse_crime and ksp is None:
                out = masks[:,None,:,:] * (fft2uc(imgs[:,None,:,:] * maps) + 1 / np.sqrt(2) * self.stdev * noise)
            else:
                if self.noncart:
                    out = out + 1 / np.sqrt(2) * self.stdev * noise
                else:
                    out = masks[None,...] * (ksp + 1 / np.sqrt(2) * self.stdev * noise)
        if not self.adjoint_data and not self.noncart:
            out = fftmod(out)
        elif self.adjoint_data and not self.noncart:
            out = np.sum(np.conj(maps) * ifft2uc(out), axis=1)
        return out


def load_data(idx, data_file, masks_file, gen_masks=False):
    with h5py.File(data_file, 'r') as F:
        imgs = np.array(F['imgs'][idx,...], dtype=np.complex64)
        maps = np.array(F['maps'][idx,...], dtype=np.complex64)

    with h5py.File(masks_file, 'r') as F:
        masks = np.array(F['masks'][idx,...], dtype=np.float32)
        loss_masks = np.array(F['loss_masks'][idx, ...], dtype=np.float32)
        if 'noise' in F.keys():
            noise = np.array(F['noise'][idx,...], dtype=np.complex64)
        else:
            noise = None
            
    # special case for batch_size=1
    if len(masks.shape) == 2:
        imgs, maps, masks, loss_masks = imgs[None,...], maps[None,...], masks[None,...], loss_masks[None,...]
        if noise is not None:
            noise = noise[None,...]
    return imgs, maps, masks, noise, loss_masks

def load_data_ksp(idx, data_file, masks_file, gen_masks=False):
    with h5py.File(data_file, 'r') as F:
        imgs = np.array(F['imgs'][idx,...], dtype=np.complex64)
        maps = np.array(F['maps'][idx,...], dtype=np.complex64)
        ksp = np.array(F['ksp'][idx,...], dtype=np.complex64)

    with h5py.File(masks_file, 'r') as F:
        if 'masks' in F.keys():
            masks = np.array(F['masks'][idx,...], dtype=np.float32)
        else:
            masks = np.array(F['mask_traj_{}'.format(idx)], dtype=np.float32)
        loss_masks = np.array(F['loss_masks'][idx, ...], dtype=np.float32)
        if 'noise' in F.keys():
            noise = np.array(F['noise'][idx,...], dtype=np.float32)
        else:
            noise = None

    # special case for batch_size=1
    if len(masks.shape) == 2:
        imgs, maps, masks, ksp, loss_masks = imgs[None,...], maps[None,...], masks[None,...], ksp[None,...], loss_masks[None,...]
        if noise is not None:
            noise = noise[None,...]
    return imgs, maps, masks, ksp, noise, loss_masks


def load_data_cached(data_file):
    with h5py.File(data_file, 'r') as F:
        imgs = np.array(F['imgs'], dtype=np.complex64)
        maps = np.array(F['maps'], dtype=np.complex64)
        masks = np.array(F['masks'], dtype=np.float32)
        loss_masks = np.array(F['loss_masks'], dtype=np.float32)
        out = np.array(F['out'], dtype=np.complex64)
    return imgs, maps, masks, out, loss_masks


def save_data_cached(data_file, imgs, maps, masks, out, loss_masks):
    with h5py.File(data_file, 'w') as F:
        F.create_dataset('imgs', data=imgs)
        F.create_dataset('maps', data=maps)
        F.create_dataset('masks', data=masks)
        F.create_dataset('out', data=out)
        F.create_dataset('loss_masks', data=loss_masks)
