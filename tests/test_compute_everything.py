"""Test that the computations on this branch agree with those on master."""

import pickle
import warnings

import numpy as np
import pytest

from desc.coils import FourierPlanarCoil, FourierRZCoil, FourierXYZCoil, SplineXYZCoil
from desc.compute import data_index
from desc.examples import get
from desc.geometry import (
    FourierPlanarCurve,
    FourierRZCurve,
    FourierRZToroidalSurface,
    FourierXYZCurve,
    ZernikeRZToroidalSection,
)
from desc.grid import LinearGrid
from desc.magnetic_fields import (
    CurrentPotentialField,
    FourierCurrentPotentialField,
    OmnigenousField,
)
from desc.utils import ResolutionWarning


def _compare_against_master(
    p, data, master_data, error=False, update_master_data=False
):

    for name in data[p]:
        if p in master_data and name in master_data[p]:
            try:
                np.testing.assert_allclose(
                    actual=data[p][name],
                    desired=master_data[p][name],
                    atol=1e-10,
                    rtol=1e-10,
                    err_msg=f"Parameterization: {p}. Name: {name}.",
                )
            except AssertionError as e:
                error = True
                print(e)
        else:  # update master data with new compute quantity
            update_master_data = True

    return error, update_master_data


@pytest.mark.unit
def test_compute_everything():
    """Test that the computations on this branch agree with those on master.

    Also make sure we can compute everything without errors. Computed quantities
    are both in "rpz" and "xyz" basis.
    """
    elliptic_cross_section_with_torsion = {
        "R_lmn": [10, 1, 0.2],
        "Z_lmn": [-2, -0.2],
        "modes_R": [[0, 0], [1, 0], [0, 1]],
        "modes_Z": [[-1, 0], [0, -1]],
    }
    things = {
        # equilibria
        "desc.equilibrium.equilibrium.Equilibrium": get("W7-X"),
        # curves
        "desc.geometry.curve.FourierXYZCurve": FourierXYZCurve(
            X_n=[5, 10, 2], Y_n=[1, 2, 3], Z_n=[-4, -5, -6]
        ),
        "desc.geometry.curve.FourierRZCurve": FourierRZCurve(
            R_n=[10, 1, 0.2], Z_n=[-2, -0.2], modes_R=[0, 1, 2], modes_Z=[-1, -2], NFP=2
        ),
        "desc.geometry.curve.FourierPlanarCurve": FourierPlanarCurve(
            center=[10, 1, 3], normal=[1, 2, 3], r_n=[1, 2, 3], modes=[0, 1, 2]
        ),
        "desc.geometry.curve.SplineXYZCurve": FourierXYZCurve(
            X_n=[5, 10, 2], Y_n=[1, 2, 3], Z_n=[-4, -5, -6]
        ).to_SplineXYZ(grid=LinearGrid(N=50)),
        # surfaces
        "desc.geometry.surface.FourierRZToroidalSurface": FourierRZToroidalSurface(
            **elliptic_cross_section_with_torsion
        ),
        "desc.geometry.surface.ZernikeRZToroidalSection": ZernikeRZToroidalSection(
            **elliptic_cross_section_with_torsion
        ),
        # magnetic fields
        "desc.magnetic_fields._current_potential.CurrentPotentialField": CurrentPotentialField(  # noqa:E501
            **elliptic_cross_section_with_torsion,
            potential=lambda theta, zeta, G: G * zeta / 2 / np.pi,
            potential_dtheta=lambda theta, zeta, G: np.zeros_like(theta),
            potential_dzeta=lambda theta, zeta, G: G * np.ones_like(theta) / 2 / np.pi,
            params={"G": 1e7},
        ),
        "desc.magnetic_fields._current_potential.FourierCurrentPotentialField": (
            FourierCurrentPotentialField(
                **elliptic_cross_section_with_torsion, I=0, G=1e7
            )
        ),
        "desc.magnetic_fields._core.OmnigenousField": OmnigenousField(
            L_B=0,
            M_B=4,
            L_x=0,
            M_x=1,
            N_x=1,
            NFP=2,
            helicity=(0, 2),
            B_lm=np.array([0.8, 0.9, 1.1, 1.2]),
            x_lmn=np.array([0, -np.pi / 8, 0, np.pi / 8, 0, np.pi / 4]),
        ),
        # coils
        "desc.coils.FourierRZCoil": FourierRZCoil(
            R_n=[10, 1, 0.2], Z_n=[-2, -0.2], modes_R=[0, 1, 2], modes_Z=[-1, -2], NFP=2
        ),
        "desc.coils.FourierXYZCoil": FourierXYZCoil(
            X_n=[5, 10, 2], Y_n=[1, 2, 3], Z_n=[-4, -5, -6]
        ),
        "desc.coils.FourierPlanarCoil": FourierPlanarCoil(
            current=5,
            center=[10, 1, 3],
            normal=[1, 2, 3],
            r_n=[1, 2, 3],
            modes=[0, 1, 2],
        ),
        "desc.coils.SplineXYZCoil": SplineXYZCoil(
            current=5, X=[5, 10, 2, 5], Y=[1, 2, 3, 1], Z=[-4, -5, -6, -4]
        ),
    }
    assert things.keys() == data_index.keys(), (
        f"Missing the parameterization {data_index.keys() - things.keys()}"
        f" to test against master."
    )
    # use this low resolution grid for equilibria to reduce file size
    eqgrid = LinearGrid(
        L=9,
        M=5,
        N=5,
        NFP=things["desc.equilibrium.equilibrium.Equilibrium"].NFP,
        sym=things["desc.equilibrium.equilibrium.Equilibrium"].sym,
        axis=True,
    )
    curvegrid1 = LinearGrid(N=10)
    curvegrid2 = LinearGrid(N=10, NFP=2)
    fieldgrid = LinearGrid(
        L=2,
        M=4,
        N=5,
        NFP=things["desc.magnetic_fields._core.OmnigenousField"].NFP,
        sym=False,
        axis=True,
    )
    grid = {
        "desc.equilibrium.equilibrium.Equilibrium": {"grid": eqgrid},
        "desc.geometry.curve.FourierXYZCurve": {"grid": curvegrid1},
        "desc.geometry.curve.FourierRZCurve": {"grid": curvegrid2},
        "desc.geometry.curve.FourierPlanarCurve": {"grid": curvegrid1},
        "desc.geometry.curve.SplineXYZCurve": {"grid": curvegrid1},
        "desc.magnetic_fields._core.OmnigenousField": {"grid": fieldgrid},
    }

    with open("tests/inputs/master_compute_data_rpz.pkl", "rb") as file:
        master_data_rpz = pickle.load(file)
    with open("tests/inputs/master_compute_data_xyz.pkl", "rb") as file:
        master_data_xyz = pickle.load(file)
    this_branch_data_rpz = {}
    this_branch_data_xyz = {}
    update_master_data_rpz = False
    update_master_data_xyz = False
    error_rpz = False
    error_xyz = False

    # some things can't compute "phi" and therefore can't convert to XYZ basis
    no_xyz_things = ["desc.magnetic_fields._core.OmnigenousField"]

    with warnings.catch_warnings():
        # Max resolution of master_compute_data.pkl limited by GitHub file
        # size cap at 100 mb, so can't hit suggested resolution for some things.
        warnings.filterwarnings("ignore", category=ResolutionWarning)
        for p in things:
            names = {
                name
                for name in data_index[p]
                # Skip these quantities as they should be covered in other tests.
                if not data_index[p][name]["source_grid_requirement"]
            }
            this_branch_data_rpz[p] = things[p].compute(
                list(names), **grid.get(p, {}), basis="rpz"
            )
            # make sure we can compute everything
            assert this_branch_data_rpz[p].keys() == names, (
                f"Parameterization: {p}. Can't compute "
                + f"{names - this_branch_data_rpz[p].keys()}."
            )
            # compare data against master branch
            error_rpz, update_master_data_rpz = _compare_against_master(
                p,
                this_branch_data_rpz,
                master_data_rpz,
                error_rpz,
                update_master_data_rpz,
            )

            # test compute in XYZ basis
            if p in no_xyz_things:
                continue
            # remove quantities that are not implemented in the XYZ basis
            # TODO: generalize this instead of hard-coding for "grad(B)" & dependencies
            names_xyz = (
                names - {"grad(B)", "|grad(B)|", "L_grad(B)"}
                if "grad(B)" in names
                else names
            )

            this_branch_data_xyz[p] = things[p].compute(
                list(names_xyz), **grid.get(p, {}), basis="xyz"
            )
            assert this_branch_data_xyz[p].keys() == names_xyz, (
                f"Parameterization: {p}. Can't compute "
                + f"{names_xyz - this_branch_data_xyz[p].keys()}."
            )
            error_xyz, update_master_data_xyz = _compare_against_master(
                p,
                this_branch_data_xyz,
                master_data_xyz,
                error_xyz,
                update_master_data_xyz,
            )

    if not error_rpz and update_master_data_rpz:
        # then update the master compute data
        with open("tests/inputs/master_compute_data_rpz.pkl", "wb") as file:
            # remember to git commit this file
            pickle.dump(this_branch_data_rpz, file)
    if not error_xyz and update_master_data_xyz:
        with open("tests/inputs/master_compute_data_xyz.pkl", "wb") as file:
            pickle.dump(this_branch_data_xyz, file)
    assert not error_rpz
    assert not error_xyz