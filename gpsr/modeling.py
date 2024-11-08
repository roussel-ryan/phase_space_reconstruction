from abc import ABC, abstractmethod
from copy import deepcopy
from typing import Tuple

import numpy as np
import torch
from torch import Tensor
from torch.utils.data import Dataset

from cheetah.accelerator import (
    Quadrupole,
    Drift,
    TransverseDeflectingCavity,
    Dipole,
    Segment,
    Screen,
)
from cheetah.particles import Beam
from phase_space_reconstruction.beams import BeamGenerator


class GPSRLattice(torch.nn.Module, ABC):
    @abstractmethod
    def set_lattice_parameters(self, x: torch.Tensor) -> None:
        pass

    @abstractmethod
    def track_and_observe(self, beam: Beam) -> Tuple[Tensor, ...]:
        """
        tracks beam through the lattice and returns observations

        Returns
        -------
        results: Tuple[Tensor]
            Tuple of results from each measurement path
        """
        pass


class GPSR(torch.nn.Module):
    def __init__(self, beam_generator: BeamGenerator, lattice: GPSRLattice):
        super(GPSR, self).__init__()
        self.beam_generator = deepcopy(beam_generator)
        self.lattice = deepcopy(lattice)

    def forward(self, x: Tensor):
        # generate beam
        initial_beam = self.beam_generator()

        # set lattice parameters
        self.lattice.set_lattice_parameters(x)

        return self.lattice.track_and_observe(initial_beam)


class GPSRQuadScanLattice(GPSRLattice):
    def __init__(self, l_quad: float, l_drift: float, diagnostic: Screen):
        super().__init__()
        q1 = Quadrupole(torch.tensor(l_quad), torch.tensor(0.0))
        d1 = Drift(torch.tensor(l_drift))
        self.lattice = Segment([q1, d1])
        self.diagnostic = diagnostic

    def track_and_observe(self, beam) -> Tuple[Tensor, ...]:
        final_beam = self.lattice(beam)
        observations = self.diagnostic(final_beam)

        return tuple(observations.unsqueeze(0))

    def set_lattice_parameters(self, x: torch.Tensor):
        self.lattice.elements[0].k1.data = x


class GPSR6DLattice(GPSRLattice):
    def __init__(
        self,
        l_quad: float,
        l_tdc: float,
        f_tdc: float,
        phi_tdc: float,
        l_bend: float,
        theta_on: float,
        l1: float,
        l2: float,
        l3: float,
        screen_1: Screen,
        screen_2: Screen,
        upstream_elements=None,
    ):
        super().__init__()

        upstream_elements = upstream_elements or []

        # Drift from Quad to TDC (0.5975)
        l_d1 = l1 - l_quad / 2 - l_tdc / 2

        # Drift from TDC to Bend (0.3392)
        l_d2 = l2 - l_tdc / 2 - l_bend / 2

        # Drift from Bend to YAG 2 (corrected for dipole on/off)
        l_d3 = l3 - l_bend / 2 / np.cos(theta_on)

        q = Quadrupole(
            torch.tensor(l_quad),
            torch.tensor(0.0),
            name="SCAN_QUAD",
            num_steps=5,
            tracking_method="bmadx",
        )
        d1 = Drift(torch.tensor(l_d1))

        tdc = TransverseDeflectingCavity(
            length=torch.tensor(l_tdc),
            voltage=torch.tensor(0.0),
            frequency=torch.tensor(f_tdc),
            phase=torch.tensor(phi_tdc),
            tilt=torch.tensor(3.14 / 2),
            name="SCAN_TDC",
        )

        d2 = Drift(length=torch.tensor(l_d2))

        # initialize with dipole on
        l_arc = l_bend * theta_on / np.sin(theta_on)

        bend = Dipole(
            name="SCAN_DIPOLE",
            length=torch.tensor(l_arc),
            angle=torch.tensor(0.0),
            e1=torch.tensor(0.0),
            e2=torch.tensor(theta_on),
        )

        d3 = Drift(name="DIPOLE_TO_SCREEN", length=torch.tensor(l_d3))

        lattice = Segment([*upstream_elements, q, d1, tdc, d2, bend, d3])

        self.l_bend = l_bend
        self.l3 = l3
        self.screen_1_diagonstic = screen_1
        self.screen_2_diagonstic = screen_2
        self.lattice = lattice

    def track_and_observe(self, beam) -> Tuple[Tensor, ...]:
        # track the beam through the accelerator in a batched way
        final_beam = self.lattice(beam)

        # observe the beam at the different diagnostics based on the first batch
        # dimension
        screen_1_observation = self.screen_1_diagonstic(final_beam[0])
        screen_2_observation = self.screen_2_diagonstic(final_beam[1])

        return screen_1_observation, screen_2_observation

    def set_lattice_parameters(self, x: torch.Tensor) -> None:
        """
        sets the quadrupole / TDC / dipole parameters

        Parameters:
        -----------
        x : Tensor
            Specifies the scan parameters in a batched manner with the
            following shape: (M x N x K x 3) where M is the number of dipole states,
            N is the number of TDC states and K is the number of quadrupole
            strengths. The elements of the final dimension correspond to the
            dipole angles, TDC voltages, and quadrupole strengths respectively

        """

        # set quad/TDC parameters
        self.lattice.SCAN_QUAD.k1.data = x[..., 2]
        self.lattice.SCAN_TDC.voltage.data = x[..., 1]

        # set dipole parameters
        G = x[..., 0]
        bend_angle = torch.arcsin(self.l_bend * G)
        arc_length = bend_angle / G
        self.lattice.SCAN_DIPOLE.angle.data = bend_angle
        self.lattice.SCAN_DIPOLE.length.data = arc_length
        self.lattice.SCAN_DIPOLE.e2.data = bend_angle

        # set parameters of drift between dipole and screen
        self.lattice.DIPOLE_TO_SCREEN.length.data = (
            self.l3 - self.l_bend / 2 / torch.cos(bend_angle)
        )