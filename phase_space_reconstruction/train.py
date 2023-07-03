import torch
from torch.utils.data import DataLoader

from phase_space_reconstruction.diagnostics import ImageDiagnostic
from phase_space_reconstruction.losses import MENTLoss
from phase_space_reconstruction.modeling import (
    NNTransform,
    InitialBeam,
    PhaseSpaceReconstructionModel,
    ImageDataset
    )


def train_quad_scan(
        train_dset,
        lattice,
        screen, 
        n_epochs = 100,
        device = 'cpu',
        save_as = None
        ):
    
    """
    Trains quad scan model.

    Parameters
    ----------
    train_data: ImageDataset
        training data

    lattice: bmadx TorchLattice
        diagnostics lattice

    screen: ImageDiagnostic
        screen diagnostics

    Returns
    -------
    predicted_beam: bmadx Beam
        reconstructed beam
        
    """
    
    # Device selection: 
    DEVICE = torch.device(device)
    print(f'Using device: {DEVICE}')

    ks = train_dset.k.to(DEVICE)
    imgs = train_dset.images.to(DEVICE)

    train_dset_device = ImageDataset(ks, imgs)
    train_dataloader = DataLoader(train_dset_device, batch_size=10, shuffle=True)

    # create phase space reconstruction model
    n_particles = 10000
    nn_transformer = NNTransform(2, 20, output_scale=1e-2)
    nn_beam = InitialBeam(
        nn_transformer,
        torch.distributions.MultivariateNormal(torch.zeros(6), torch.eye(6)),
        n_particles,
        p0c=torch.tensor(10.0e6),
    )
    model = PhaseSpaceReconstructionModel(
        lattice,
        screen,
        nn_beam
    )

    model = model.to(DEVICE)

    # train model
    optimizer = torch.optim.Adam(model.parameters(), lr=0.01)
    loss_fn = MENTLoss(torch.tensor(1e11))

    for i in range(n_epochs):
        for elem in train_dataloader:
            k, target_images = elem[0], elem[1]

            optimizer.zero_grad()
            output = model(k)
            loss = loss_fn(output, target_images)
            loss.backward()
            optimizer.step()

        if i % 100 == 0:
            print(i, loss)

    model = model.to('cpu')

    predicted_beam = model.beam.forward().detach()

    if save_as is not None:
        torch.save(predicted_beam, save_as)
    
    return predicted_beam
    