###########################################################################################
# Training script
# Authors: Ilyes Batatia, Gregor Simm, David Kovacs
# This program is distributed under the ASL License (see ASL.md)
###########################################################################################

import dataclasses
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import torch
from torch.optim.swa_utils import SWALR, AveragedModel
from torch.utils.data import DataLoader
from torch_ema import ExponentialMovingAverage

from . import torch_geometric
from .checkpoint import CheckpointHandler, CheckpointState
from .torch_tools import tensor_dict_to_device, to_numpy
from .utils import (
    MetricsLogger,
    compute_mae,
    compute_q95,
    compute_rel_mae,
    compute_rel_rmse,
    compute_rmse,
)


@dataclasses.dataclass
class SWAContainer:
    model: AveragedModel
    scheduler: SWALR
    start: int
    loss_fn: torch.nn.Module


def train(
    model: torch.nn.Module,
    loss_fn: torch.nn.Module,
    train_loader: DataLoader,
    valid_loader: DataLoader,
    optimizer: torch.optim.Optimizer,
    lr_scheduler: torch.optim.lr_scheduler.ExponentialLR,
    start_epoch: int,
    max_num_epochs: int,
    patience: int,
    checkpoint_handler: CheckpointHandler,
    logger: MetricsLogger,
    eval_interval: int,
    output_args: Dict[str, bool],
    device: torch.device,
    log_errors: str,
    swa: Optional[SWAContainer] = None,
    ema: Optional[ExponentialMovingAverage] = None,
    max_grad_norm: Optional[float] = 10.0,
):
    lowest_loss = np.inf
    patience_counter = 0
    swa_start = True

    if max_grad_norm is not None:
        logging.info(f"Using gradient clipping with tolerance={max_grad_norm:.3f}")
    logging.info("Started training")
    for epoch in range(start_epoch, max_num_epochs):
        # Train
        for batch in train_loader:
            _, opt_metrics = take_step(
                model=model,
                loss_fn=loss_fn,
                batch=batch,
                optimizer=optimizer,
                ema=ema,
                output_args=output_args,
                max_grad_norm=max_grad_norm,
                device=device,
            )
            opt_metrics["mode"] = "opt"
            opt_metrics["epoch"] = epoch
            logger.log(opt_metrics)

        # Validate
        if epoch % eval_interval == 0:
            if ema is not None:
                with ema.average_parameters():
                    valid_loss, eval_metrics = evaluate(
                        model=model,
                        loss_fn=loss_fn,
                        data_loader=valid_loader,
                        output_args=output_args,
                        device=device,
                    )
            else:
                valid_loss, eval_metrics = evaluate(
                    model=model,
                    loss_fn=loss_fn,
                    data_loader=valid_loader,
                    output_args=output_args,
                    device=device,
                )
            eval_metrics["mode"] = "eval"
            eval_metrics["epoch"] = epoch
            logger.log(eval_metrics)
            if log_errors == "PerAtomRMSE":
                error_e = eval_metrics["rmse_e_per_atom"] * 1e3
                error_f = eval_metrics["rmse_f"] * 1e3
                logging.info(
                    f"Epoch {epoch}: loss={valid_loss:.4f}, RMSE_E_per_atom={error_e:.1f} meV, RMSE_F={error_f:.1f} meV / A"
                )
            elif log_errors == "PerAtomRMSEstressvirials":
                error_e = eval_metrics["rmse_e_per_atom"] * 1e3
                error_f = eval_metrics["rmse_f"] * 1e3
                error_stress = eval_metrics["rmse_stress"] * 1e3
                error_virials = eval_metrics["rmse_virials"] * 1e3
                logging.info(
                    f"Epoch {epoch}: loss={valid_loss:.4f}, RMSE_E_per_atom={error_e:.1f} meV, RMSE_F={error_f:.1f} meV / A, RMSE_stress={error_stress:.1f} meV / A,RMSE_virials={error_virials:.1f} meV / A"
                )
            elif log_errors == "TotalRMSE":
                error_e = eval_metrics["rmse_e"] * 1e3
                error_f = eval_metrics["rmse_f"] * 1e3
                logging.info(
                    f"Epoch {epoch}: loss={valid_loss:.4f}, RMSE_E={error_e:.1f} meV, RMSE_F={error_f:.1f} meV / A"
                )
            elif log_errors == "PerAtomMAE":
                error_e = eval_metrics["mae_e_per_atom"] * 1e3
                error_f = eval_metrics["mae_f"] * 1e3
                logging.info(
                    f"Epoch {epoch}: loss={valid_loss:.4f}, MAE_E_per_atom={error_e:.1f} meV, MAE_F={error_f:.1f} meV / A"
                )
            elif log_errors == "TotalMAE":
                error_e = eval_metrics["mae_e"] * 1e3
                error_f = eval_metrics["mae_f"] * 1e3
                logging.info(
                    f"Epoch {epoch}: loss={valid_loss:.4f}, MAE_E={error_e:.1f} meV, MAE_F={error_f:.1f} meV / A"
                )
            if valid_loss >= lowest_loss:
                patience_counter += 1
                if patience_counter >= patience:
                    logging.info(
                        f"Stopping optimization after {patience_counter} epochs without improvement"
                    )
                    break
            else:
                lowest_loss = valid_loss
                patience_counter = 0
                if ema is not None:
                    with ema.average_parameters():
                        checkpoint_handler.save(
                            state=CheckpointState(model, optimizer, lr_scheduler),
                            epochs=epoch,
                        )
                else:
                    checkpoint_handler.save(
                        state=CheckpointState(model, optimizer, lr_scheduler),
                        epochs=epoch,
                    )

        # LR scheduler and SWA update
        if swa is None or epoch < swa.start:
            lr_scheduler.step(valid_loss)  # Can break if exponential LR, TODO fix that!
        else:
            if swa_start:
                logging.info("Changing loss based on SWA")
                swa_start = False
            loss_fn = swa.loss_fn
            swa.model.update_parameters(model)
            swa.scheduler.step()

    logging.info("Training complete")


def take_step(
    model: torch.nn.Module,
    loss_fn: torch.nn.Module,
    batch: torch_geometric.batch.Batch,
    optimizer: torch.optim.Optimizer,
    ema: Optional[ExponentialMovingAverage],
    output_args: Dict[str, bool],
    max_grad_norm: Optional[float],
    device: torch.device,
) -> Tuple[float, Dict[str, Any]]:

    start_time = time.time()
    batch = batch.to(device)
    optimizer.zero_grad()
    output = model(
        batch,
        training=False,
        compute_force=output_args["forces"],
        compute_virials=output_args["virials"],
        compute_stress=output_args["stress"],
    )
    loss = loss_fn(pred=output, ref=batch)
    loss.backward()
    if max_grad_norm is not None:
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=max_grad_norm)
    optimizer.step()

    if ema is not None:
        ema.update()

    loss_dict = {
        "loss": to_numpy(loss),
        "time": time.time() - start_time,
    }

    return loss, loss_dict


def evaluate(
    model: torch.nn.Module,
    loss_fn: torch.nn.Module,
    data_loader: DataLoader,
    output_args: Dict[str, bool],
    device: torch.device,
) -> Tuple[float, Dict[str, Any]]:
    total_loss = 0.0
    delta_es_list = []
    delta_es_per_atom_list = []
    delta_fs_list = []
    fs_list = []
    delta_stress_list = []
    delta_virials_list = []

    start_time = time.time()
    for batch in data_loader:
        batch = batch.to(device)
        output = model(
            batch,
            training=False,
            compute_force=output_args["forces"],
            compute_virials=output_args["virials"],
            compute_stress=output_args["stress"],
        )
        batch = batch.cpu()
        output = tensor_dict_to_device(output, device=torch.device("cpu"))

        loss = loss_fn(pred=output, ref=batch)
        total_loss += to_numpy(loss).item()

        if output["energy"] is not None:
            delta_es_list.append(batch.energy - output["energy"])
            delta_es_per_atom_list.append(
                (batch.energy - output["energy"]) / (batch.ptr[1:] - batch.ptr[:-1])
            )
        if output["forces"] is not None:
            delta_fs_list.append(batch.forces - output["forces"])
            fs_list.append(batch.forces)
        if output["stress"] is not None and batch.stress is not None:
            delta_stress_list.append(batch.stress - output["stress"])
        if output["virials"] is not None and batch.virials is not None:
            delta_virials_list.append(batch.virials - output["virials"])

    avg_loss = total_loss / len(data_loader)

    delta_es = (
        to_numpy(torch.cat(delta_es_list, dim=0))
        if output["energy"] is not None
        else None
    )
    delta_es_per_atom = (
        to_numpy(torch.cat(delta_es_per_atom_list, dim=0))
        if output["energy"] is not None
        else None
    )
    delta_fs = (
        to_numpy(torch.cat(delta_fs_list, dim=0))
        if output["forces"] is not None
        else None
    )
    fs = to_numpy(torch.cat(fs_list, dim=0)) if output["forces"] is not None else None
    delta_stress = (
        to_numpy(torch.cat(delta_stress_list, dim=0))
        if output["stress"] is not None
        else None
    )
    delta_virials = (
        to_numpy(torch.cat(delta_virials_list, dim=0))
        if output["virials"] is not None
        else None
    )

    aux = {
        "loss": avg_loss,
        # Mean absolute error
        "mae_e": compute_mae(delta_es) if delta_es is not None else None,
        "mae_e_per_atom": compute_mae(delta_es_per_atom)
        if delta_es_per_atom is not None
        else None,
        "mae_f": compute_mae(delta_fs) if delta_fs is not None else None,
        "rel_mae_f": compute_rel_mae(delta_fs, fs) if delta_fs is not None else None,
        "mae_stress": compute_mae(delta_stress) if delta_stress is not None else None,
        "mae_virials": compute_mae(delta_virials)
        if delta_virials is not None
        else None,
        # Root-mean-square error
        "rmse_e": compute_rmse(delta_es) if delta_es is not None else None,
        "rmse_e_per_atom": compute_rmse(delta_es_per_atom)
        if delta_es_per_atom is not None
        else None,
        "rmse_f": compute_rmse(delta_fs) if delta_fs is not None else None,
        "rel_rmse_f": compute_rel_rmse(delta_fs, fs) if delta_fs is not None else None,
        "rmse_stress": compute_rmse(delta_stress) if delta_stress is not None else None,
        "rmse_virials": compute_rmse(delta_virials)
        if delta_virials is not None
        else None,
        # Q_95
        "q95_e": compute_q95(delta_es) if delta_es is not None else None,
        "q95_f": compute_q95(delta_fs) if delta_fs is not None else None,
        "rmse_stress": compute_q95(delta_stress) if delta_stress is not None else None,
        "rmse_virials": compute_q95(delta_virials)
        if delta_virials is not None
        else None,
        # Time
        "time": time.time() - start_time,
    }

    return avg_loss, aux
