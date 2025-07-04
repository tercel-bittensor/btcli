import asyncio
import binascii
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import timedelta
import functools
import hashlib
import io
import math
import multiprocessing as mp
from multiprocessing.queues import Queue as Queue_Type
from multiprocessing import Process, Event, Lock, Array, Value, Queue
import os
from queue import Empty, Full
import random
import time
import typing
from typing import Optional
import subprocess

from bittensor_wallet import Wallet
from Crypto.Hash import keccak
import numpy as np
from rich.prompt import Confirm
from rich.console import Console
from rich.status import Status
from async_substrate_interface.errors import SubstrateRequestException

from bittensor_cli.src import COLOR_PALETTE
from bittensor_cli.src.bittensor.chain_data import NeuronInfo
from bittensor_cli.src.bittensor.balances import Balance
from bittensor_cli.src.bittensor.utils import (
    console,
    err_console,
    format_error_message,
    millify,
    get_human_readable,
    print_verbose,
    print_error,
    unlock_key,
    hex_to_bytes,
)

if typing.TYPE_CHECKING:
    from bittensor_cli.src.bittensor.subtensor_interface import SubtensorInterface


def use_torch() -> bool:
    """Force the use of torch over numpy for certain operations."""
    return True if os.getenv("USE_TORCH") == "1" else False


def legacy_torch_api_compat(func: typing.Callable):
    """
    Convert function operating on numpy Input&Output to legacy torch Input&Output API if `use_torch()` is True.

    :param func: Function with numpy Input/Output to be decorated.

    :return: Decorated function
    """

    @functools.wraps(func)
    def decorated(*args, **kwargs):
        if use_torch():
            # if argument is a Torch tensor, convert it to numpy
            args = [
                arg.cpu().numpy() if isinstance(arg, torch.Tensor) else arg
                for arg in args
            ]
            kwargs = {
                key: value.cpu().numpy() if isinstance(value, torch.Tensor) else value
                for key, value in kwargs.items()
            }
        ret = func(*args, **kwargs)
        if use_torch():
            # if return value is a numpy array, convert it to Torch tensor
            if isinstance(ret, np.ndarray):
                ret = torch.from_numpy(ret)
        return ret

    return decorated


@functools.cache
def _get_real_torch():
    try:
        import torch as _real_torch
    except ImportError:
        _real_torch = None
    return _real_torch


def log_no_torch_error():
    err_console.print(
        "This command requires torch. You can install torch"
        " with `pip install torch` and run the command again."
    )


@dataclass
class POWSolution:
    """A solution to the registration PoW problem."""

    nonce: int
    block_number: int
    difficulty: int
    seal: bytes

    async def is_stale(self, subtensor: "SubtensorInterface") -> bool:
        """Returns True if the POW is stale.
        This means the block the POW is solved for is within 3 blocks of the current block.
        """
        current_block = await subtensor.substrate.get_block_number(None)
        return self.block_number < current_block - 3


@dataclass
class RegistrationStatistics:
    """Statistics for a registration."""

    time_spent_total: float
    rounds_total: int
    time_average: float
    time_spent: float
    hash_rate_perpetual: float
    hash_rate: float
    difficulty: int
    block_number: int
    block_hash: str


class RegistrationStatisticsLogger:
    """Logs statistics for a registration."""

    console: Console
    status: Optional[Status]

    def __init__(self, console_: Console, output_in_place: bool = True) -> None:
        self.console = console_

        if output_in_place:
            self.status = self.console.status("Solving")
        else:
            self.status = None

    def start(self) -> None:
        if self.status is not None:
            self.status.start()

    def stop(self) -> None:
        if self.status is not None:
            self.status.stop()

    @classmethod
    def get_status_message(
        cls, stats: RegistrationStatistics, verbose: bool = False
    ) -> str:
        """
        Provides a message of the current status of the block solving as a str for a logger or stdout
        """
        message = (
            "Solving\n"
            + f"Time Spent (total): [bold white]{timedelta(seconds=stats.time_spent_total)}[/bold white]\n"
            + (
                f"Time Spent This Round: {timedelta(seconds=stats.time_spent)}\n"
                + f"Time Spent Average: {timedelta(seconds=stats.time_average)}\n"
                if verbose
                else ""
            )
            + f"Registration Difficulty: [bold white]{millify(stats.difficulty)}[/bold white]\n"
            + f"Iters (Inst/Perp): [bold white]{get_human_readable(stats.hash_rate, 'H')}/s / "
            + f"{get_human_readable(stats.hash_rate_perpetual, 'H')}/s[/bold white]\n"
            + f"Block Number: [bold white]{stats.block_number}[/bold white]\n"
            + f"Block Hash: [bold white]{stats.block_hash.encode('utf-8')}[/bold white]\n"
        )
        return message

    def update(self, stats: RegistrationStatistics, verbose: bool = False) -> None:
        """
        Passes the current status to the logger
        """
        if self.status is not None:
            self.status.update(self.get_status_message(stats, verbose=verbose))
        else:
            self.console.log(self.get_status_message(stats, verbose=verbose))


class _SolverBase(Process):
    """
    A process that solves the registration PoW problem.

    :param proc_num: The number of the process being created.
    :param num_proc: The total number of processes running.
    :param update_interval: The number of nonces to try to solve before checking for a new block.
    :param finished_queue: The queue to put the process number when a process finishes each update_interval.
                           Used for calculating the average time per update_interval across all processes.
    :param solution_queue: The queue to put the solution the process has found during the pow solve.
    :param stop_event: The event to set by the main process when all the solver processes should stop.
                       The solver process will check for the event after each update_interval.
                       The solver process will stop when the event is set.
                       Used to stop the solver processes when a solution is found.
    :param curr_block: The array containing this process's current block hash.
                       The main process will set the array to the new block hash when a new block is finalized in the
                       network. The solver process will get the new block hash from this array when newBlockEvent is set
    :param curr_block_num: The value containing this process's current block number.
                           The main process will set the value to the new block number when a new block is finalized in
                           the network. The solver process will get the new block number from this value when
                           new_block_event is set.
    :param curr_diff: The array containing this process's current difficulty. The main process will set the array to
                      the new difficulty when a new block is finalized in the network. The solver process will get the
                      new difficulty from this array when newBlockEvent is set.
    :param check_block: The lock to prevent this process from getting the new block data while the main process is
                        updating the data.
    :param limit: The limit of the pow solve for a valid solution.

    :var new_block_event: The event to set by the main process when a new block is finalized in the network.
                          The solver process will check for the event after each update_interval.
                          The solver process will get the new block hash and difficulty and start solving for a new
                          nonce.
    """

    proc_num: int
    num_proc: int
    update_interval: int
    finished_queue: Queue_Type
    solution_queue: Queue_Type
    new_block_event: Event
    stop_event: Event
    hotkey_bytes: bytes
    curr_block: Array
    curr_block_num: Value
    curr_diff: Array
    check_block: Lock
    limit: int

    def __init__(
        self,
        proc_num,
        num_proc,
        update_interval,
        finished_queue,
        solution_queue,
        stop_event,
        curr_block,
        curr_block_num,
        curr_diff,
        check_block,
        limit,
    ):
        Process.__init__(self, daemon=True)
        self.proc_num = proc_num
        self.num_proc = num_proc
        self.update_interval = update_interval
        self.finished_queue = finished_queue
        self.solution_queue = solution_queue
        self.new_block_event = Event()
        self.new_block_event.clear()
        self.curr_block = curr_block
        self.curr_block_num = curr_block_num
        self.curr_diff = curr_diff
        self.check_block = check_block
        self.stop_event = stop_event
        self.limit = limit

    def run(self):
        raise NotImplementedError("_SolverBase is an abstract class")

    @staticmethod
    def create_shared_memory() -> tuple[Array, Value, Array]:
        """Creates shared memory for the solver processes to use."""
        curr_block = Array("h", 32, lock=True)  # byte array
        curr_block_num = Value("i", 0, lock=True)  # int
        curr_diff = Array("Q", [0, 0], lock=True)  # [high, low]

        return curr_block, curr_block_num, curr_diff


class _Solver(_SolverBase):
    """
    Performs POW Solution
    """

    def run(self):
        block_number: int
        block_and_hotkey_hash_bytes: bytes
        block_difficulty: int
        nonce_limit = int(math.pow(2, 64)) - 1

        # Start at random nonce
        nonce_start = random.randint(0, nonce_limit)
        nonce_end = nonce_start + self.update_interval
        while not self.stop_event.is_set():
            if self.new_block_event.is_set():
                with self.check_block:
                    block_number = self.curr_block_num.value
                    block_and_hotkey_hash_bytes = bytes(self.curr_block)
                    block_difficulty = _registration_diff_unpack(self.curr_diff)

                self.new_block_event.clear()

            # Do a block of nonces
            solution = _solve_for_nonce_block(
                nonce_start,
                nonce_end,
                block_and_hotkey_hash_bytes,
                block_difficulty,
                self.limit,
                block_number,
            )
            if solution is not None:
                self.solution_queue.put(solution)

            try:
                # Send time
                self.finished_queue.put_nowait(self.proc_num)
            except Full:
                pass

            nonce_start = random.randint(0, nonce_limit)
            nonce_start = nonce_start % nonce_limit
            nonce_end = nonce_start + self.update_interval


class _CUDASolver(_SolverBase):
    """
    Performs POW Solution using CUDA
    """

    dev_id: int
    tpb: int

    def __init__(
        self,
        proc_num,
        num_proc,
        update_interval,
        finished_queue,
        solution_queue,
        stop_event,
        curr_block,
        curr_block_num,
        curr_diff,
        check_block,
        limit,
        dev_id: int,
        tpb: int,
    ):
        super().__init__(
            proc_num,
            num_proc,
            update_interval,
            finished_queue,
            solution_queue,
            stop_event,
            curr_block,
            curr_block_num,
            curr_diff,
            check_block,
            limit,
        )
        self.dev_id = dev_id
        self.tpb = tpb

    def run(self):
        block_number: int = 0  # dummy value
        block_and_hotkey_hash_bytes: bytes = b"0" * 32  # dummy value
        block_difficulty: int = int(math.pow(2, 64)) - 1  # dummy value
        nonce_limit = int(math.pow(2, 64)) - 1  # U64MAX

        # Start at random nonce
        nonce_start = random.randint(0, nonce_limit)
        while not self.stop_event.is_set():
            if self.new_block_event.is_set():
                with self.check_block:
                    block_number = self.curr_block_num.value
                    block_and_hotkey_hash_bytes = bytes(self.curr_block)
                    block_difficulty = _registration_diff_unpack(self.curr_diff)

                self.new_block_event.clear()

            # Do a block of nonces
            solution = _solve_for_nonce_block_cuda(
                nonce_start,
                self.update_interval,
                block_and_hotkey_hash_bytes,
                block_difficulty,
                self.limit,
                block_number,
                self.dev_id,
                self.tpb,
            )
            if solution is not None:
                self.solution_queue.put(solution)

            try:
                # Signal that a nonce_block was finished using queue
                # send our proc_num
                self.finished_queue.put(self.proc_num)
            except Full:
                pass

            # increase nonce by number of nonces processed
            nonce_start += self.update_interval * self.tpb
            nonce_start = nonce_start % nonce_limit


class LazyLoadedTorch:
    def __bool__(self):
        return bool(_get_real_torch())

    def __getattr__(self, name):
        if real_torch := _get_real_torch():
            return getattr(real_torch, name)
        else:
            log_no_torch_error()
            raise ImportError("torch not installed")


if typing.TYPE_CHECKING:
    import torch
else:
    torch = LazyLoadedTorch()


class MaxSuccessException(Exception):
    """
    Raised when the POW Solver has reached the max number of successful solutions
    """


class MaxAttemptsException(Exception):
    """
    Raised when the POW Solver has reached the max number of attempts
    """


async def is_hotkey_registered(
    subtensor: "SubtensorInterface", netuid: int, hotkey_ss58: str
) -> bool:
    """Checks to see if the hotkey is registered on a given netuid"""
    _result = await subtensor.query(
        module="SubtensorModule",
        storage_function="Uids",
        params=[netuid, hotkey_ss58],
    )
    if _result is not None:
        return True
    else:
        return False


async def register_extrinsic(
    subtensor: "SubtensorInterface",
    wallet: Wallet,
    netuid: int,
    wait_for_inclusion: bool = False,
    wait_for_finalization: bool = True,
    prompt: bool = False,
    max_allowed_attempts: int = 3,
    output_in_place: bool = True,
    cuda: bool = False,
    dev_id: typing.Union[list[int], int] = 0,
    tpb: int = 256,
    num_processes: Optional[int] = None,
    update_interval: Optional[int] = None,
    log_verbose: bool = False,
) -> bool:
    """Registers the wallet to the chain.

    :param subtensor: initialized SubtensorInterface object to use for chain interactions
    :param wallet: Bittensor wallet object.
    :param netuid: The ``netuid`` of the subnet to register on.
    :param wait_for_inclusion: If set, waits for the extrinsic to enter a block before returning `True`, or returns
                               `False` if the extrinsic fails to enter the block within the timeout.
    :param wait_for_finalization: If set, waits for the extrinsic to be finalized on the chain before returning `True`,
                                 or returns `False` if the extrinsic fails to be finalized within the timeout.
    :param prompt: If `True`, the call waits for confirmation from the user before proceeding.
    :param max_allowed_attempts: Maximum number of attempts to register the wallet.
    :param output_in_place: Whether the POW solving should be outputted to the console as it goes along.
    :param cuda: If `True`, the wallet should be registered using CUDA device(s).
    :param dev_id: The CUDA device id to use, or a list of device ids.
    :param tpb: The number of threads per block (CUDA).
    :param num_processes: The number of processes to use to register.
    :param update_interval: The number of nonces to solve between updates.
    :param log_verbose: If `True`, the registration process will log more information.

    :return: `True` if extrinsic was finalized or included in the block. If we did not wait for finalization/inclusion,
             the response is `True`.
    """

    async def get_neuron_for_pubkey_and_subnet():
        uid = await subtensor.query(
            "SubtensorModule", "Uids", [netuid, wallet.hotkey.ss58_address]
        )
        if uid is None:
            return NeuronInfo.get_null_neuron()

        result = await subtensor.neuron_for_uid(
            uid=uid,
            netuid=netuid,
            block_hash=subtensor.substrate.last_block_hash,
        )
        return result

    print_verbose("Checking subnet status")
    if not await subtensor.subnet_exists(netuid):
        err_console.print(
            f":cross_mark: [red]Failed[/red]: error: [bold white]subnet:{netuid}[/bold white] does not exist."
        )
        return False

    with console.status(
        f":satellite: Checking Account on [bold]subnet:{netuid}[/bold]...",
        spinner="aesthetic",
    ) as status:
        neuron = await get_neuron_for_pubkey_and_subnet()
        if not neuron.is_null:
            print_error(
                f"Wallet {wallet} is already registered on subnet {neuron.netuid} with uid {neuron.uid}",
                status,
            )
            return True

    if prompt:
        if not Confirm.ask(
            f"Continue Registration?\n"
            f"  hotkey [{COLOR_PALETTE.G.HK}]({wallet.hotkey_str})[/{COLOR_PALETTE.G.HK}]:"
            f"\t[{COLOR_PALETTE.G.HK}]{wallet.hotkey.ss58_address}[/{COLOR_PALETTE.G.HK}]\n"
            f"  coldkey [{COLOR_PALETTE.G.CK}]({wallet.name})[/{COLOR_PALETTE.G.CK}]:"
            f"\t[{COLOR_PALETTE.G.CK}]{wallet.coldkeypub.ss58_address}[/{COLOR_PALETTE.G.CK}]\n"
            f"  network:\t\t[{COLOR_PALETTE.G.LINKS}]{subtensor.network}[/{COLOR_PALETTE.G.LINKS}]\n"
        ):
            return False

    if not torch:
        log_no_torch_error()
        return False

    # Attempt rolling registration.
    attempts = 1
    pow_result: Optional[POWSolution]
    while True:
        console.print(
            ":satellite: Registering...({}/{})".format(attempts, max_allowed_attempts)
        )
        # Solve latest POW.
        if cuda:
            if not torch.cuda.is_available():
                if prompt:
                    console.print("CUDA is not available.")
                return False
            pow_result = await create_pow(
                subtensor,
                wallet,
                netuid,
                output_in_place,
                cuda=cuda,
                dev_id=dev_id,
                tpb=tpb,
                num_processes=num_processes,
                update_interval=update_interval,
                log_verbose=log_verbose,
            )
        else:
            pow_result = await create_pow(
                subtensor,
                wallet,
                netuid,
                output_in_place,
                cuda=cuda,
                num_processes=num_processes,
                update_interval=update_interval,
                log_verbose=log_verbose,
            )

        # pow failed
        if not pow_result:
            # might be registered already on this subnet
            is_registered = await is_hotkey_registered(
                subtensor, netuid=netuid, hotkey_ss58=wallet.hotkey.ss58_address
            )
            if is_registered:
                err_console.print(
                    f":white_heavy_check_mark: [dark_sea_green3]Already registered on netuid:{netuid}[/dark_sea_green3]"
                )
                return True

        # pow successful, proceed to submit pow to chain for registration
        else:
            with console.status(":satellite: Submitting POW..."):
                # check if pow result is still valid
                while not await pow_result.is_stale(subtensor=subtensor):
                    call = await subtensor.substrate.compose_call(
                        call_module="SubtensorModule",
                        call_function="register",
                        call_params={
                            "netuid": netuid,
                            "block_number": pow_result.block_number,
                            "nonce": pow_result.nonce,
                            "work": [int(byte_) for byte_ in pow_result.seal],
                            "hotkey": wallet.hotkey.ss58_address,
                            "coldkey": wallet.coldkeypub.ss58_address,
                        },
                    )
                    extrinsic = await subtensor.substrate.create_signed_extrinsic(
                        call=call, keypair=wallet.hotkey
                    )
                    response = await subtensor.substrate.submit_extrinsic(
                        extrinsic,
                        wait_for_inclusion=wait_for_inclusion,
                        wait_for_finalization=wait_for_finalization,
                    )
                    if not wait_for_finalization and not wait_for_inclusion:
                        success, err_msg = True, ""
                    else:
                        success = await response.is_success
                        if not success:
                            success, err_msg = (
                                False,
                                format_error_message(await response.error_message),
                            )
                            # Look error here
                            # https://github.com/opentensor/subtensor/blob/development/pallets/subtensor/src/errors.rs

                            if "HotKeyAlreadyRegisteredInSubNet" in err_msg:
                                console.print(
                                    f":white_heavy_check_mark: [dark_sea_green3]Already Registered on "
                                    f"[bold]subnet:{netuid}[/bold][/dark_sea_green3]"
                                )
                                return True
                            err_console.print(
                                f":cross_mark: [red]Failed[/red]: {err_msg}"
                            )
                            await asyncio.sleep(0.5)

                    # Successful registration, final check for neuron and pubkey
                    if success:
                        console.print(":satellite: Checking Registration status...")
                        is_registered = await is_hotkey_registered(
                            subtensor,
                            netuid=netuid,
                            hotkey_ss58=wallet.hotkey.ss58_address,
                        )
                        if is_registered:
                            console.print(
                                ":white_heavy_check_mark: [dark_sea_green3]Registered[/dark_sea_green3]"
                            )
                            return True
                        else:
                            # neuron not found, try again
                            err_console.print(
                                ":cross_mark: [red]Unknown error. Neuron not found.[/red]"
                            )
                            continue
                else:
                    # Exited loop because pow is no longer valid.
                    err_console.print("[red]POW is stale.[/red]")
                    # Try again.
                    continue

        if attempts < max_allowed_attempts:
            # Failed registration, retry pow
            attempts += 1
            err_console.print(
                ":satellite: Failed registration, retrying pow ...({attempts}/{max_allowed_attempts})"
            )
        else:
            # Failed to register after max attempts.
            err_console.print("[red]No more attempts.[/red]")
            return False


async def burned_register_extrinsic(
    subtensor: "SubtensorInterface",
    wallet: Wallet,
    netuid: int,
    old_balance: Balance,
    wait_for_inclusion: bool = True,
    wait_for_finalization: bool = True,
    era: Optional[int] = None,
) -> tuple[bool, str]:
    """Registers the wallet to chain by recycling TAO.

    :param subtensor: The SubtensorInterface object to use for the call, initialized
    :param wallet: Bittensor wallet object.
    :param netuid: The `netuid` of the subnet to register on.
    :param old_balance: The wallet balance prior to the registration burn.
    :param wait_for_inclusion: If set, waits for the extrinsic to enter a block before returning `True`, or returns
                               `False` if the extrinsic fails to enter the block within the timeout.
    :param wait_for_finalization: If set, waits for the extrinsic to be finalized on the chain before returning `True`,
                                  or returns `False` if the extrinsic fails to be finalized within the timeout.
    :param era: the period (in blocks) for which the transaction should remain valid.
    :param prompt: If `True`, the call waits for confirmation from the user before proceeding.

    :return: (success, msg), where success is `True` if extrinsic was finalized or included in the block. If we did not
        wait for finalization/inclusion, the response is `True`.
    """

    if not (unlock_status := unlock_key(wallet, print_out=False)).success:
        return False, unlock_status.message

    with console.status(
        f":satellite: Checking Account on [bold]subnet:{netuid}[/bold]...",
        spinner="aesthetic",
    ) as status:
        my_uid = await subtensor.query(
            "SubtensorModule", "Uids", [netuid, wallet.hotkey.ss58_address]
        )
        block_hash = await subtensor.substrate.get_chain_head()

        print_verbose("Checking if already registered", status)
        neuron = await subtensor.neuron_for_uid(
            uid=my_uid, netuid=netuid, block_hash=block_hash
        )
        if not era:
            current_block, tempo, blocks_since_last_step = await asyncio.gather(
                subtensor.substrate.get_block_number(block_hash=block_hash),
                subtensor.get_hyperparameter(
                    "Tempo", netuid=netuid, block_hash=block_hash
                ),
                subtensor.query(
                    "SubtensorModule",
                    "BlocksSinceLastStep",
                    [netuid],
                    block_hash=block_hash,
                ),
            )
            validity_period = tempo - blocks_since_last_step
            era_ = {
                "period": validity_period,
                "current": current_block,
            }
        else:
            era_ = {"period": era}

    if not neuron.is_null:
        console.print(
            ":white_heavy_check_mark: [dark_sea_green3]Already Registered[/dark_sea_green3]:\n"
            f"uid: [{COLOR_PALETTE.G.NETUID_EXTRA}]{neuron.uid}[/{COLOR_PALETTE.G.NETUID_EXTRA}]\n"
            f"netuid: [{COLOR_PALETTE.G.NETUID}]{neuron.netuid}[/{COLOR_PALETTE.G.NETUID}]\n"
            f"hotkey: [{COLOR_PALETTE.G.HK}]{neuron.hotkey}[/{COLOR_PALETTE.G.HK}]\n"
            f"coldkey: [{COLOR_PALETTE.G.CK}]{neuron.coldkey}[/{COLOR_PALETTE.G.CK}]"
        )
        return True, "Already registered"

    with console.status(
        ":satellite: Recycling TAO for Registration...", spinner="aesthetic"
    ):
        call = await subtensor.substrate.compose_call(
            call_module="SubtensorModule",
            call_function="burned_register",
            call_params={
                "netuid": netuid,
                "hotkey": wallet.hotkey.ss58_address,
            },
        )
        success, err_msg = await subtensor.sign_and_send_extrinsic(
            call, wallet, wait_for_inclusion, wait_for_finalization, era=era_
        )

    if not success:
        err_console.print(f":cross_mark: [red]Failed[/red]: {err_msg}")
        await asyncio.sleep(0.5)
        return False, err_msg
    # Successful registration, final check for neuron and pubkey
    else:
        with console.status(":satellite: Checking Balance...", spinner="aesthetic"):
            block_hash = await subtensor.substrate.get_chain_head()
            new_balance, netuids_for_hotkey, my_uid = await asyncio.gather(
                subtensor.get_balance(
                    wallet.coldkeypub.ss58_address,
                    block_hash=block_hash,
                    reuse_block=False,
                ),
                subtensor.get_netuids_for_hotkey(
                    wallet.hotkey.ss58_address, block_hash=block_hash
                ),
                subtensor.query(
                    "SubtensorModule", "Uids", [netuid, wallet.hotkey.ss58_address]
                ),
            )

        console.print(
            "Balance:\n"
            f"  [blue]{old_balance}[/blue] :arrow_right: "
            f"[{COLOR_PALETTE.S.STAKE_AMOUNT}]{new_balance}[/{COLOR_PALETTE.S.STAKE_AMOUNT}]"
        )

        if len(netuids_for_hotkey) > 0:
            console.print(
                f":white_heavy_check_mark: [green]Registered on netuid {netuid} with UID {my_uid}[/green]"
            )
            return True, f"Registered on {netuid} with UID {my_uid}"
        else:
            # neuron not found, try again
            err_console.print(
                ":cross_mark: [red]Unknown error. Neuron not found.[/red]"
            )
            return False, "Unknown error. Neuron not found."


async def run_faucet_extrinsic(
    subtensor: "SubtensorInterface",
    wallet: Wallet,
    wait_for_inclusion: bool = False,
    wait_for_finalization: bool = True,
    prompt: bool = False,
    max_allowed_attempts: int = 3,
    output_in_place: bool = True,
    cuda: bool = False,
    dev_id: int = 0,
    tpb: int = 256,
    num_processes: Optional[int] = None,
    update_interval: Optional[int] = None,
    log_verbose: bool = True,
    max_successes: int = 3,
) -> tuple[bool, str]:
    r"""Runs a continual POW to get a faucet of TAO on the test net.

    :param subtensor: The subtensor interface object used to run the extrinsic
    :param wallet: Bittensor wallet object.
    :param prompt: If `True`, the call waits for confirmation from the user before proceeding.
    :param wait_for_inclusion: If set, waits for the extrinsic to enter a block before returning `True`,
                               or returns `False` if the extrinsic fails to enter the block within the timeout.
    :param wait_for_finalization: If set, waits for the extrinsic to be finalized on the chain before returning `True`,
                                  or returns `False` if the extrinsic fails to be finalized within the timeout.
    :param max_allowed_attempts: Maximum number of attempts to register the wallet.
    :param output_in_place: Whether to output logging data as the process runs.
    :param cuda: If `True`, the wallet should be registered using CUDA device(s).
    :param dev_id: The CUDA device id to use
    :param tpb: The number of threads per block (CUDA).
    :param num_processes: The number of processes to use to register.
    :param update_interval: The number of nonces to solve between updates.
    :param log_verbose: If `True`, the registration process will log more information.
    :param max_successes: The maximum number of successful faucet runs for the wallet.

    :return: `True` if extrinsic was finalized or included in the block. If we did not wait for
                    finalization/inclusion, the response is also `True`
    """
    if prompt:
        if not Confirm.ask(
            "Run Faucet?\n"
            f" wallet name: [bold white]{wallet.name}[/bold white]\n"
            f" coldkey:    [bold white]{wallet.coldkeypub.ss58_address}[/bold white]\n"
            f" network:    [bold white]{subtensor}[/bold white]"
        ):
            return False, ""

    if not torch:
        log_no_torch_error()
        return False, "Requires torch"

    # Unlock coldkey
    if not (unlock_status := unlock_key(wallet, print_out=False)).success:
        return False, unlock_status.message

    # Get previous balance.
    old_balance = await subtensor.get_balance(wallet.coldkeypub.ss58_address)

    # Attempt rolling registration.
    attempts = 1
    successes = 1
    pow_result: Optional[POWSolution]
    while True:
        try:
            account_nonce = await subtensor.substrate.get_account_nonce(
                wallet.coldkey.ss58_address
            )
            pow_result = None
            while pow_result is None or await pow_result.is_stale(subtensor=subtensor):
                # Solve latest POW.
                if cuda:
                    if not torch.cuda.is_available():
                        if prompt:
                            err_console.print("CUDA is not available.")
                        return False, "CUDA is not available."
                    pow_result = await create_pow(
                        subtensor,
                        wallet,
                        -1,
                        output_in_place,
                        cuda=cuda,
                        dev_id=dev_id,
                        tpb=tpb,
                        num_processes=num_processes,
                        update_interval=update_interval,
                        log_verbose=log_verbose,
                    )
                else:
                    pow_result = await create_pow(
                        subtensor,
                        wallet,
                        -1,
                        output_in_place,
                        cuda=cuda,
                        num_processes=num_processes,
                        update_interval=update_interval,
                        log_verbose=log_verbose,
                    )
            call = await subtensor.substrate.compose_call(
                call_module="SubtensorModule",
                call_function="faucet",
                call_params={
                    "block_number": pow_result.block_number,
                    "nonce": pow_result.nonce,
                    "work": [int(byte_) for byte_ in pow_result.seal],
                },
            )
            extrinsic = await subtensor.substrate.create_signed_extrinsic(
                call=call, keypair=wallet.coldkey, nonce=account_nonce
            )
            response = await subtensor.substrate.submit_extrinsic(
                extrinsic,
                wait_for_inclusion=wait_for_inclusion,
                wait_for_finalization=wait_for_finalization,
            )

            # process if registration successful, try again if pow is still valid
            if not await response.is_success:
                err_console.print(
                    f":cross_mark: [red]Failed[/red]: "
                    f"{format_error_message(await response.error_message)}"
                )
                if attempts == max_allowed_attempts:
                    raise MaxAttemptsException
                attempts += 1
                # Wait a bit before trying again
                time.sleep(1)

            # Successful registration
            else:
                new_balance = await subtensor.get_balance(
                    wallet.coldkeypub.ss58_address
                )
                console.print(
                    f"Balance: [blue]{old_balance}[/blue] :arrow_right:"
                    f" [green]{new_balance}[/green]"
                )
                old_balance = new_balance

                if successes == max_successes:
                    raise MaxSuccessException

                attempts = 1  # Reset attempts on success
                successes += 1

        except KeyboardInterrupt:
            return True, "Done"

        except MaxSuccessException:
            return True, f"Max successes reached: {3}"

        except MaxAttemptsException:
            return False, f"Max attempts reached: {max_allowed_attempts}"


async def _check_for_newest_block_and_update(
    subtensor: "SubtensorInterface",
    netuid: int,
    old_block_number: int,
    hotkey_bytes: bytes,
    curr_diff: Array,
    curr_block: Array,
    curr_block_num: Value,
    update_curr_block: typing.Callable,
    check_block: Lock,
    solvers: list[_Solver],
    curr_stats: RegistrationStatistics,
) -> int:
    """
    Checks for a new block and updates the current block information if a new block is found.

    :param subtensor: The subtensor object to use for getting the current block.
    :param netuid: The netuid to use for retrieving the difficulty.
    :param old_block_number: The old block number to check against.
    :param hotkey_bytes: The bytes of the hotkey's pubkey.
    :param curr_diff: The current difficulty as a multiprocessing array.
    :param curr_block: Where the current block is stored as a multiprocessing array.
    :param curr_block_num: Where the current block number is stored as a multiprocessing value.
    :param update_curr_block: A function that updates the current block.
    :param check_block: A mp lock that is used to check for a new block.
    :param solvers: A list of solvers to update the current block for.
    :param curr_stats: The current registration statistics to update.

    :return: The current block number.
    """
    block_number = await subtensor.substrate.get_block_number(None)
    if block_number != old_block_number:
        old_block_number = block_number
        # update block information
        block_number, difficulty, block_hash = await _get_block_with_retry(
            subtensor=subtensor, netuid=netuid
        )
        block_bytes = hex_to_bytes(block_hash)

        update_curr_block(
            curr_diff,
            curr_block,
            curr_block_num,
            block_number,
            block_bytes,
            difficulty,
            hotkey_bytes,
            check_block,
        )
        # Set new block events for each solver

        for worker in solvers:
            worker.new_block_event.set()

        # update stats
        curr_stats.block_number = block_number
        curr_stats.block_hash = block_hash
        curr_stats.difficulty = difficulty

    return old_block_number


async def _block_solver(
    subtensor: "SubtensorInterface",
    wallet: Wallet,
    num_processes: int,
    netuid: int,
    dev_id: list[int],
    tpb: int,
    update_interval: int,
    curr_block,
    curr_block_num,
    curr_diff,
    n_samples,
    alpha_,
    output_in_place,
    log_verbose,
    cuda: bool,
):
    """
    Shared code used by the Solvers to solve the POW solution
    """
    limit = int(math.pow(2, 256)) - 1

    # Establish communication queues
    # See the _Solver class for more information on the queues.
    stop_event = Event()
    stop_event.clear()

    solution_queue = Queue()
    if cuda:
        num_processes = len(dev_id)

    finished_queues = [Queue() for _ in range(num_processes)]
    check_block = Lock()

    hotkey_bytes = (
        wallet.coldkeypub.public_key if netuid == -1 else wallet.hotkey.public_key
    )

    if cuda:
        # Create a worker per CUDA device
        solvers = [
            _CUDASolver(
                i,
                num_processes,
                update_interval,
                finished_queues[i],
                solution_queue,
                stop_event,
                curr_block,
                curr_block_num,
                curr_diff,
                check_block,
                limit,
                dev_id[i],
                tpb,
            )
            for i in range(num_processes)
        ]
    else:
        # Start consumers
        solvers = [
            _Solver(
                i,
                num_processes,
                update_interval,
                finished_queues[i],
                solution_queue,
                stop_event,
                curr_block,
                curr_block_num,
                curr_diff,
                check_block,
                limit,
            )
            for i in range(num_processes)
        ]

    # Get first block
    block_number, difficulty, block_hash = await _get_block_with_retry(
        subtensor=subtensor, netuid=netuid
    )

    block_bytes = hex_to_bytes(block_hash)
    old_block_number = block_number
    # Set to current block
    _update_curr_block(
        curr_diff,
        curr_block,
        curr_block_num,
        block_number,
        block_bytes,
        difficulty,
        hotkey_bytes,
        check_block,
    )

    # Set new block events for each solver to start at the initial block
    for worker in solvers:
        worker.new_block_event.set()

    for worker in solvers:
        worker.start()  # start the solver processes

    start_time = time.time()  # time that the registration started
    time_last = start_time  # time that the last work blocks completed

    curr_stats = RegistrationStatistics(
        time_spent_total=0.0,
        time_average=0.0,
        rounds_total=0,
        time_spent=0.0,
        hash_rate_perpetual=0.0,
        hash_rate=0.0,
        difficulty=difficulty,
        block_number=block_number,
        block_hash=block_hash,
    )

    start_time_perpetual = time.time()

    logger = RegistrationStatisticsLogger(console, output_in_place)
    logger.start()

    solution = None

    hash_rates = [0] * n_samples  # The last n true hash_rates
    weights = [alpha_**i for i in range(n_samples)]  # weights decay by alpha

    timeout = 0.15 if cuda else 0.15
    while netuid == -1 or not await is_hotkey_registered(
        subtensor, netuid, wallet.hotkey.ss58_address
    ):
        # Wait until a solver finds a solution
        try:
            solution = solution_queue.get(block=True, timeout=timeout)
            if solution is not None:
                break
        except Empty:
            # No solution found, try again
            pass

        # check for new block
        old_block_number = await _check_for_newest_block_and_update(
            subtensor=subtensor,
            netuid=netuid,
            hotkey_bytes=hotkey_bytes,
            old_block_number=old_block_number,
            curr_diff=curr_diff,
            curr_block=curr_block,
            curr_block_num=curr_block_num,
            curr_stats=curr_stats,
            update_curr_block=_update_curr_block,
            check_block=check_block,
            solvers=solvers,
        )

        num_time = 0
        for finished_queue in finished_queues:
            try:
                finished_queue.get(timeout=0.1)
                num_time += 1

            except Empty:
                continue

        time_now = time.time()  # get current time
        time_since_last = time_now - time_last  # get time since last work block(s)
        if num_time > 0 and time_since_last > 0.0:
            # create EWMA of the hash_rate to make measure more robust

            if cuda:
                hash_rate_ = (num_time * tpb * update_interval) / time_since_last
            else:
                hash_rate_ = (num_time * update_interval) / time_since_last
            hash_rates.append(hash_rate_)
            hash_rates.pop(0)  # remove the 0th data point
            curr_stats.hash_rate = sum(
                [hash_rates[i] * weights[i] for i in range(n_samples)]
            ) / (sum(weights))

            # update time last to now
            time_last = time_now

            curr_stats.time_average = (
                curr_stats.time_average * curr_stats.rounds_total
                + curr_stats.time_spent
            ) / (curr_stats.rounds_total + num_time)
            curr_stats.rounds_total += num_time

        # Update stats
        curr_stats.time_spent = time_since_last
        new_time_spent_total = time_now - start_time_perpetual
        if cuda:
            curr_stats.hash_rate_perpetual = (
                curr_stats.rounds_total * (tpb * update_interval)
            ) / new_time_spent_total
        else:
            curr_stats.hash_rate_perpetual = (
                curr_stats.rounds_total * update_interval
            ) / new_time_spent_total
        curr_stats.time_spent_total = new_time_spent_total

        # Update the logger
        logger.update(curr_stats, verbose=log_verbose)

    # exited while, solution contains the nonce or wallet is registered
    stop_event.set()  # stop all other processes
    logger.stop()

    # terminate and wait for all solvers to exit
    _terminate_workers_and_wait_for_exit(solvers)

    return solution


async def _solve_for_difficulty_fast_cuda(
    subtensor: "SubtensorInterface",
    wallet: Wallet,
    netuid: int,
    output_in_place: bool = True,
    update_interval: int = 50_000,
    tpb: int = 512,
    dev_id: typing.Union[list[int], int] = 0,
    n_samples: int = 10,
    alpha_: float = 0.80,
    log_verbose: bool = False,
) -> Optional[POWSolution]:
    """
    Solves the registration fast using CUDA

    :param subtensor: The subtensor node to grab blocks
    :param wallet: The wallet to register
    :param netuid: The netuid of the subnet to register to.
    :param output_in_place: If true, prints the output in place, otherwise prints to new lines
    :param update_interval: The number of nonces to try before checking for more blocks
    :param tpb: The number of threads per block. CUDA param that should match the GPU capability
    :param dev_id: The CUDA device IDs to execute the registration on, either a single device or a list of devices
    :param n_samples: The number of samples of the hash_rate to keep for the EWMA
    :param alpha_: The alpha for the EWMA for the hash_rate calculation
    :param log_verbose: If true, prints more verbose logging of the registration metrics.

    Note: The hash rate is calculated as an exponentially weighted moving average in order to make the measure more
          robust.
    """
    if isinstance(dev_id, int):
        dev_id = [dev_id]
    elif dev_id is None:
        dev_id = [0]

    if update_interval is None:
        update_interval = 50_000

    if not torch.cuda.is_available():
        raise Exception("CUDA not available")

    # Set mp start to use spawn so CUDA doesn't complain
    with _UsingSpawnStartMethod(force=True):
        curr_block, curr_block_num, curr_diff = _CUDASolver.create_shared_memory()

        solution = await _block_solver(
            subtensor=subtensor,
            wallet=wallet,
            num_processes=None,
            netuid=netuid,
            dev_id=dev_id,
            tpb=tpb,
            update_interval=update_interval,
            curr_block=curr_block,
            curr_block_num=curr_block_num,
            curr_diff=curr_diff,
            n_samples=n_samples,
            alpha_=alpha_,
            output_in_place=output_in_place,
            log_verbose=log_verbose,
            cuda=True,
        )

        return solution


async def _solve_for_difficulty_fast(
    subtensor,
    wallet: Wallet,
    netuid: int,
    output_in_place: bool = True,
    num_processes: Optional[int] = None,
    update_interval: Optional[int] = None,
    n_samples: int = 10,
    alpha_: float = 0.80,
    log_verbose: bool = False,
) -> Optional[POWSolution]:
    """
    Solves the POW for registration using multiprocessing.

    :param subtensor: Subtensor to connect to for block information and to submit.
    :param wallet: wallet to use for registration.
    :param netuid: The netuid of the subnet to register to.
    :param output_in_place: If true, prints the status in place. Otherwise, prints the status on a new line.
    :param num_processes: Number of processes to use.
    :param update_interval: Number of nonces to solve before updating block information.
    :param n_samples: The number of samples of the hash_rate to keep for the EWMA
    :param alpha_: The alpha for the EWMA for the hash_rate calculation
    :param log_verbose: If true, prints more verbose logging of the registration metrics.

    Notes:

    - The hash rate is calculated as an exponentially weighted moving average in order to make the measure more robust.
    - We can also modify the update interval to do smaller blocks of work, while still updating the block information
      after a different number of nonces, to increase the transparency of the process while still keeping the speed.
    """
    if not num_processes:
        # get the number of allowed processes for this process
        num_processes = min(1, get_cpu_count())

    if update_interval is None:
        update_interval = 50_000

    curr_block, curr_block_num, curr_diff = _Solver.create_shared_memory()

    solution = await _block_solver(
        subtensor=subtensor,
        wallet=wallet,
        num_processes=num_processes,
        netuid=netuid,
        dev_id=None,
        tpb=None,
        update_interval=update_interval,
        curr_block=curr_block,
        curr_block_num=curr_block_num,
        curr_diff=curr_diff,
        n_samples=n_samples,
        alpha_=alpha_,
        output_in_place=output_in_place,
        log_verbose=log_verbose,
        cuda=False,
    )

    return solution


def _terminate_workers_and_wait_for_exit(
    workers: list[typing.Union[Process, Queue_Type]],
) -> None:
    for worker in workers:
        if isinstance(worker, Queue_Type):
            worker.join_thread()
        else:
            try:
                worker.join(3.0)
            except subprocess.TimeoutExpired:
                worker.terminate()
        try:
            worker.close()
        except ValueError:
            worker.terminate()


async def _get_block_with_retry(
    subtensor: "SubtensorInterface", netuid: int
) -> tuple[int, int, str]:
    """
    Gets the current block number, difficulty, and block hash from the substrate node.

    :param subtensor: The subtensor object to use to get the block number, difficulty, and block hash.
    :param netuid: The netuid of the network to get the block number, difficulty, and block hash from.

    :return: The current block number, difficulty of the subnet, block hash

    :raises Exception: If the block hash is None.
    :raises ValueError: If the difficulty is None.
    """
    block = await subtensor.substrate.get_block()
    block_hash = block["header"]["hash"]
    block_number = block["header"]["number"]
    try:
        difficulty = (
            1_000_000
            if netuid == -1
            else int(
                await subtensor.get_hyperparameter(
                    param_name="Difficulty", netuid=netuid, block_hash=block_hash
                )
            )
        )
    except TypeError:
        raise ValueError("Chain error. Difficulty is None")
    except SubstrateRequestException:
        raise Exception(
            "Network error. Could not connect to substrate to get block hash"
        )
    return block_number, difficulty, block_hash


def _registration_diff_unpack(packed_diff: Array) -> int:
    """Unpacks the packed two 32-bit integers into one 64-bit integer. Little endian."""
    return int(packed_diff[0] << 32 | packed_diff[1])


def _registration_diff_pack(diff: int, packed_diff: Array):
    """Packs the difficulty into two 32-bit integers. Little endian."""
    packed_diff[0] = diff >> 32
    packed_diff[1] = diff & 0xFFFFFFFF  # low 32 bits


class _UsingSpawnStartMethod:
    def __init__(self, force: bool = False):
        self._old_start_method = None
        self._force = force

    def __enter__(self):
        self._old_start_method = mp.get_start_method(allow_none=True)
        if self._old_start_method is None:
            self._old_start_method = "spawn"  # default to spawn

        mp.set_start_method("spawn", force=self._force)

    def __exit__(self, *args):
        # restore the old start method
        mp.set_start_method(self._old_start_method, force=True)


async def create_pow(
    subtensor: "SubtensorInterface",
    wallet: Wallet,
    netuid: int,
    output_in_place: bool = True,
    cuda: bool = False,
    dev_id: typing.Union[list[int], int] = 0,
    tpb: int = 256,
    num_processes: int = None,
    update_interval: int = None,
    log_verbose: bool = False,
) -> Optional[dict[str, typing.Any]]:
    """
    Creates a proof of work for the given subtensor and wallet.

    :param subtensor: The subtensor to create a proof of work for.
    :param wallet: The wallet to create a proof of work for.
    :param netuid: The netuid for the subnet to create a proof of work for.
    :param output_in_place: If true, prints the progress of the proof of work to the console
                            in-place. Meaning the progress is printed on the same lines.
    :param cuda: If true, uses CUDA to solve the proof of work.
    :param dev_id: The CUDA device id(s) to use. If cuda is true and dev_id is a list,
                   then multiple CUDA devices will be used to solve the proof of work.
    :param tpb: The number of threads per block to use when solving the proof of work. Should be a multiple of 32.
    :param num_processes: The number of processes to use when solving the proof of work.
                          If None, then the number of processes is equal to the number of CPU cores.
    :param update_interval: The number of nonces to run before checking for a new block.
    :param log_verbose: If true, prints the progress of the proof of work more verbosely.

    :return: The proof of work solution or None if the wallet is already registered or there is a different error.

    :raises ValueError: If the subnet does not exist.
    """
    if netuid != -1:
        if not await subtensor.subnet_exists(netuid=netuid):
            raise ValueError(f"Subnet {netuid} does not exist")

    if cuda:
        solution: Optional[POWSolution] = await _solve_for_difficulty_fast_cuda(
            subtensor,
            wallet,
            netuid=netuid,
            output_in_place=output_in_place,
            dev_id=dev_id,
            tpb=tpb,
            update_interval=update_interval,
            log_verbose=log_verbose,
        )
    else:
        solution: Optional[POWSolution] = await _solve_for_difficulty_fast(
            subtensor,
            wallet,
            netuid=netuid,
            output_in_place=output_in_place,
            num_processes=num_processes,
            update_interval=update_interval,
            log_verbose=log_verbose,
        )

    return solution


def _solve_for_nonce_block_cuda(
    nonce_start: int,
    update_interval: int,
    block_and_hotkey_hash_bytes: bytes,
    difficulty: int,
    limit: int,
    block_number: int,
    dev_id: int,
    tpb: int,
) -> Optional[POWSolution]:
    """
    Tries to solve the POW on a CUDA device for a block of nonces (nonce_start, nonce_start + update_interval * tpb
    """
    solution, seal = solve_cuda(
        nonce_start,
        update_interval,
        tpb,
        block_and_hotkey_hash_bytes,
        difficulty,
        limit,
        dev_id,
    )

    if solution != -1:
        # Check if solution is valid (i.e. not -1)
        return POWSolution(solution, block_number, difficulty, seal)

    return None


def _solve_for_nonce_block(
    nonce_start: int,
    nonce_end: int,
    block_and_hotkey_hash_bytes: bytes,
    difficulty: int,
    limit: int,
    block_number: int,
) -> Optional[POWSolution]:
    """
    Tries to solve the POW for a block of nonces (nonce_start, nonce_end)
    """
    for nonce in range(nonce_start, nonce_end):
        # Create seal.
        seal = _create_seal_hash(block_and_hotkey_hash_bytes, nonce)

        # Check if seal meets difficulty
        if _seal_meets_difficulty(seal, difficulty, limit):
            # Found a solution, save it.
            return POWSolution(nonce, block_number, difficulty, seal)

    return None


class CUDAException(Exception):
    """An exception raised when an error occurs in the CUDA environment."""


def _hex_bytes_to_u8_list(hex_bytes: bytes):
    hex_chunks = [int(hex_bytes[i : i + 2], 16) for i in range(0, len(hex_bytes), 2)]
    return hex_chunks


def _create_seal_hash(block_and_hotkey_hash_bytes: bytes, nonce: int) -> bytes:
    """
    Create a cryptographic seal hash from the given block and hotkey hash bytes and nonce.

    This function generates a seal hash by combining the given block and hotkey hash bytes with a nonce.
    It first converts the nonce to a byte representation, then concatenates it with the first 64 hex
    characters of the block and hotkey hash bytes. The result is then hashed using SHA-256 followed by
    the Keccak-256 algorithm to produce the final seal hash.

    :param block_and_hotkey_hash_bytes: The combined hash bytes of the block and hotkey.
    :param nonce: The nonce value used for hashing.

    :return: The resulting seal hash.
    """
    nonce_bytes = binascii.hexlify(nonce.to_bytes(8, "little"))
    pre_seal = nonce_bytes + binascii.hexlify(block_and_hotkey_hash_bytes)[:64]
    seal_sh256 = hashlib.sha256(bytearray(_hex_bytes_to_u8_list(pre_seal))).digest()
    kec = keccak.new(digest_bits=256)
    seal = kec.update(seal_sh256).digest()
    return seal


def _seal_meets_difficulty(seal: bytes, difficulty: int, limit: int) -> bool:
    """Determines if a seal meets the specified difficulty"""
    seal_number = int.from_bytes(seal, "big")
    product = seal_number * difficulty
    return product < limit


def _hash_block_with_hotkey(block_bytes: bytes, hotkey_bytes: bytes) -> bytes:
    """Hashes the block with the hotkey using Keccak-256 to get 32 bytes"""
    kec = keccak.new(digest_bits=256)
    kec = kec.update(bytearray(block_bytes + hotkey_bytes))
    block_and_hotkey_hash_bytes = kec.digest()
    return block_and_hotkey_hash_bytes


def _update_curr_block(
    curr_diff: Array,
    curr_block: Array,
    curr_block_num: Value,
    block_number: int,
    block_bytes: bytes,
    diff: int,
    hotkey_bytes: bytes,
    lock: Lock,
):
    """
    Update the current block data with the provided block information and difficulty.

    This function updates the current block
    and its difficulty in a thread-safe manner. It sets the current block
    number, hashes the block with the hotkey, updates the current block bytes, and packs the difficulty.

    :param curr_diff: Shared array to store the current difficulty.
    :param curr_block: Shared array to store the current block data.
    :param curr_block_num: Shared value to store the current block number.
    :param block_number: The block number to set as the current block number.
    :param block_bytes: The block data bytes to be hashed with the hotkey.
    :param diff: The difficulty value to be packed into the current difficulty array.
    :param hotkey_bytes: The hotkey bytes used for hashing the block.
    :param lock: A lock to ensure thread-safe updates.
    """
    with lock:
        curr_block_num.value = block_number
        # Hash the block with the hotkey
        block_and_hotkey_hash_bytes = _hash_block_with_hotkey(block_bytes, hotkey_bytes)
        for i in range(32):
            curr_block[i] = block_and_hotkey_hash_bytes[i]
        _registration_diff_pack(diff, curr_diff)


def get_cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        # macOS does not have sched_getaffinity
        return os.cpu_count()


@dataclass
class RegistrationStatistics:
    """Statistics for a registration."""

    time_spent_total: float
    rounds_total: int
    time_average: float
    time_spent: float
    hash_rate_perpetual: float
    hash_rate: float
    difficulty: int
    block_number: int
    block_hash: bytes


def solve_cuda(
    nonce_start: np.int64,
    update_interval: np.int64,
    tpb: int,
    block_and_hotkey_hash_bytes: bytes,
    difficulty: int,
    limit: int,
    dev_id: int = 0,
) -> tuple[np.int64, bytes]:
    """
    Solves the PoW problem using CUDA.

    :param nonce_start: Starting nonce.
    :param update_interval: Number of nonces to solve before updating block information.
    :param tpb: Threads per block.
    :param block_and_hotkey_hash_bytes: Keccak(Bytes of the block hash + bytes of the hotkey) 64 bytes.
    :param difficulty: Difficulty of the PoW problem.
    :param limit: Upper limit of the nonce.
    :param dev_id: The CUDA device ID

    :return: (nonce, seal) corresponding to the solution. Returns -1 for nonce if no solution is found.
    """

    try:
        import cubit
    except ImportError:
        raise ImportError("Please install cubit")

    upper = int(limit // difficulty)

    upper_bytes = upper.to_bytes(32, byteorder="little", signed=False)

    # Call cython function
    # int blockSize, uint64 nonce_start, uint64 update_interval, const unsigned char[:] limit,
    # const unsigned char[:] block_bytes, int dev_id
    block_and_hotkey_hash_hex = binascii.hexlify(block_and_hotkey_hash_bytes)[:64]

    solution = cubit.solve_cuda(
        tpb,
        nonce_start,
        update_interval,
        upper_bytes,
        block_and_hotkey_hash_hex,
        dev_id,
    )  # 0 is first GPU
    seal = None
    if solution != -1:
        seal = _create_seal_hash(block_and_hotkey_hash_hex, solution)
        if _seal_meets_difficulty(seal, difficulty, limit):
            return solution, seal
        else:
            return -1, b"\x00" * 32

    return solution, seal


def reset_cuda():
    """
    Resets the CUDA environment.
    """
    try:
        import cubit
    except ImportError:
        raise ImportError("Please install cubit")

    cubit.reset_cuda()


def log_cuda_errors() -> str:
    """
    Logs any CUDA errors.
    """
    try:
        import cubit
    except ImportError:
        raise ImportError("Please install cubit")

    f = io.StringIO()
    with redirect_stdout(f):
        cubit.log_cuda_errors()

    s = f.getvalue()

    return s


async def swap_hotkey_extrinsic(
    subtensor: "SubtensorInterface",
    wallet: Wallet,
    new_wallet: Wallet,
    netuid: Optional[int] = None,
    prompt: bool = False,
) -> bool:
    """
    Performs an extrinsic update for swapping two hotkeys on the chain

    :return: Success
    """
    block_hash = await subtensor.substrate.get_chain_head()
    netuids_registered = await subtensor.get_netuids_for_hotkey(
        wallet.hotkey.ss58_address, block_hash=block_hash
    )
    netuids_registered_new_hotkey = await subtensor.get_netuids_for_hotkey(
        new_wallet.hotkey.ss58_address, block_hash=block_hash
    )

    if netuid is not None and netuid not in netuids_registered:
        err_console.print(
            f":cross_mark: [red]Failed[/red]: Original hotkey {wallet.hotkey.ss58_address} is not registered on subnet {netuid}"
        )
        return False

    elif not len(netuids_registered) > 0:
        err_console.print(
            f"Original hotkey [dark_orange]{wallet.hotkey.ss58_address}[/dark_orange] is not registered on any subnet. "
            f"Please register and try again"
        )
        return False

    if netuid is not None:
        if netuid in netuids_registered_new_hotkey:
            err_console.print(
                f":cross_mark: [red]Failed[/red]: New hotkey {new_wallet.hotkey.ss58_address} "
                f"is already registered on subnet {netuid}"
            )
            return False
    else:
        if len(netuids_registered_new_hotkey) > 0:
            err_console.print(
                f":cross_mark: [red]Failed[/red]: New hotkey {new_wallet.hotkey.ss58_address} "
                f"is already registered on subnet(s) {netuids_registered_new_hotkey}"
            )
            return False

    if not unlock_key(wallet).success:
        return False

    if prompt:
        # Prompt user for confirmation.
        if netuid is not None:
            confirm_message = (
                f"Do you want to swap [dark_orange]{wallet.name}[/dark_orange] hotkey \n\t"
                f"[dark_orange]{wallet.hotkey.ss58_address} ({wallet.hotkey_str})[/dark_orange] with hotkey \n\t"
                f"[dark_orange]{new_wallet.hotkey.ss58_address} ({new_wallet.hotkey_str})[/dark_orange] on subnet {netuid}\n"
                "This operation will cost [bold cyan]1 TAO (recycled)[/bold cyan]"
            )
        else:
            confirm_message = (
                f"Do you want to swap [dark_orange]{wallet.name}[/dark_orange] hotkey \n\t"
                f"[dark_orange]{wallet.hotkey.ss58_address} ({wallet.hotkey_str})[/dark_orange] with hotkey \n\t"
                f"[dark_orange]{new_wallet.hotkey.ss58_address} ({new_wallet.hotkey_str})[/dark_orange] on all subnets\n"
                "This operation will cost [bold cyan]1 TAO (recycled)[/bold cyan]"
            )

        if not Confirm.ask(confirm_message):
            return False
    print_verbose(
        f"Swapping {wallet.name}'s hotkey ({wallet.hotkey.ss58_address} - {wallet.hotkey_str}) with "
        f"{new_wallet.name}'s hotkey ({new_wallet.hotkey.ss58_address} - {new_wallet.hotkey_str})"
    )
    with console.status(":satellite: Swapping hotkeys...", spinner="aesthetic"):
        call_params = {
            "hotkey": wallet.hotkey.ss58_address,
            "new_hotkey": new_wallet.hotkey.ss58_address,
            "netuid": netuid,
        }

        call = await subtensor.substrate.compose_call(
            call_module="SubtensorModule",
            call_function="swap_hotkey",
            call_params=call_params,
        )
        success, err_msg = await subtensor.sign_and_send_extrinsic(call, wallet)

        if success:
            console.print(
                f"Hotkey {wallet.hotkey.ss58_address} ({wallet.hotkey_str}) swapped for new hotkey: {new_wallet.hotkey.ss58_address} ({new_wallet.hotkey_str})"
            )
            return True
        else:
            err_console.print(f":cross_mark: [red]Failed[/red]: {err_msg}")
            time.sleep(0.5)
            return False
