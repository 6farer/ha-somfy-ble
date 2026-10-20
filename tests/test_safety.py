from __future__ import annotations

import sys
from dataclasses import is_dataclass
from pathlib import Path
from typing import Optional, get_type_hints
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from custom_components.somfy_ble.models import (  # noqa: E402
    AdmittedCommand,
    BeginResult,
    COMMAND_TTL_SECONDS,
    CommandRequest,
    CompletionResult,
    EnqueueResult,
    MAX_QUEUE_DEPTH,
    MAX_TRACKED_RECORDS,
    Result,
    result_name,
)
from custom_components.somfy_ble.protocol import Command  # noqa: E402
from custom_components.somfy_ble.safety import Controller  # noqa: E402


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FaultClock:
    def __init__(self, value: object = 0.0) -> None:
        self.value = value

    def __call__(self) -> object:
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class SafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.controller = Controller(self.clock)
        self.controller.set_configured_motors(("living", "bedroom"))

    def request(self, motor: str, command: Command, *, step: int = 100) -> CommandRequest:
        return CommandRequest(motor_id=motor, command=command, step=step)


    def test_clock_regression_expires_queued_work(self) -> None:
        self.clock.now = 100.0
        queued = self.controller.enqueue(self.request("living", Command.JOG))
        self.clock.now = 99.0
        self.assertEqual(self.controller.begin_next().result, Result.EXPIRED)
        self.assertEqual(self.controller.begin_next().result, Result.NOT_FOUND)
        self.assertEqual(queued.command_id, 1)

    def test_clock_anomaly_preserves_preempted_results_until_drain(self) -> None:
        for anomaly in (99.0, float("nan")):
            with self.subTest(anomaly=anomaly):
                controller = Controller(self.clock)
                controller.set_configured_motors(("living", "bedroom"))
                self.clock.now = 100.0
                queued = [
                    controller.enqueue(self.request("living", Command.IDENTIFY))
                    for _ in range(MAX_QUEUE_DEPTH - 1)
                ]
                stop = controller.enqueue(self.request("living", Command.STOP))
                self.assertEqual(stop.result, Result.SUCCESS)
                self.clock.now = anomaly
                self.assertEqual(
                    controller.enqueue(self.request("bedroom", Command.IDENTIFY)).result,
                    Result.BUSY,
                )
                self.assertEqual(
                    controller.drain_deferred_results(),
                    tuple(
                        CompletionResult(Result.PREEMPTED, item.command_id)
                        for item in queued
                    ),
                )
                self.clock.now = 101.0
                self.assertEqual(
                    controller.enqueue(self.request("bedroom", Command.IDENTIFY)).result,
                    Result.SUCCESS,
                )

    def test_command_ids_remain_unique_across_reboot_cleanup_and_stale_callbacks(self) -> None:
        old = self.controller.enqueue(self.request("living", Command.IDENTIFY))
        self.assertEqual(old.command_id, 1)
        self.assertEqual(self.controller.begin_next().command_id, old.command_id)
        self.controller.on_reboot()
        self.assertEqual(
            self.controller.drain_deferred_results(),
            (CompletionResult(Result.ERROR, old.command_id),),
        )

        new = self.controller.enqueue(self.request("bedroom", Command.IDENTIFY))
        self.assertNotEqual(new.command_id, old.command_id)
        self.assertEqual(self.controller.begin_next().command_id, new.command_id)
        self.assertEqual(self.controller.prepare_write(old.command_id), Result.STALE_CALLBACK)
        self.assertFalse(self.controller.mark_write_attempted(old.command_id))
        self.assertEqual(
            self.controller.complete(old.command_id, Result.SUCCESS),
            CompletionResult(Result.STALE_CALLBACK, old.command_id),
        )
        self.assertTrue(self.controller.has_active())
        self.assertEqual(
            self.controller.complete(new.command_id, Result.SUCCESS),
            CompletionResult(Result.SUCCESS, new.command_id),
        )


    def test_unknown_motor_and_invalid_payload_are_machine_readable(self) -> None:
        unknown = self.controller.enqueue(self.request("missing", Command.IDENTIFY))
        self.assertEqual(unknown.result, Result.NOT_FOUND)
        invalid = self.controller.enqueue(self.request("living", Command.JOG, step=0))
        self.assertEqual(invalid.result, Result.INVALID_PAYLOAD)
        self.assertEqual(self.controller.pending_count(), 0)

    def test_command_records_are_immutable_and_do_not_contain_credentials(self) -> None:
        self.controller.enqueue(self.request("living", Command.IDENTIFY))
        started = self.controller.begin_next()
        assert started.command is not None
        with self.assertRaises((AttributeError, TypeError)):
            started.command.request = self.request("bedroom", Command.STOP)  # type: ignore[misc]
        self.assertNotIn("pin", started.command.__slots__)

    def test_public_boundary_records_are_immutable_slotted_and_unambiguous(self) -> None:
        records = (CommandRequest, AdmittedCommand, EnqueueResult, BeginResult, CompletionResult)
        for record in records:
            self.assertTrue(is_dataclass(record))
            self.assertTrue(hasattr(record, "__slots__"))
            self.assertNotIn("__dict__", record.__slots__)

        self.assertIs(get_type_hints(CommandRequest)["command"], Command)
        self.assertIs(get_type_hints(AdmittedCommand)["request"], CommandRequest)
        self.assertEqual(
            get_type_hints(BeginResult)["command"], Optional[AdmittedCommand]
        )

        request = self.request("living", Command.IDENTIFY)
        self.assertFalse(hasattr(request, "__dict__"))
        with self.assertRaises((AttributeError, TypeError)):
            request.motor_id = "bedroom"  # type: ignore[misc]

    def test_queue_is_bounded_to_eight_and_active_counts_toward_budget(self) -> None:
        for _ in range(8):
            self.assertEqual(
                self.controller.enqueue(self.request("living", Command.IDENTIFY)).result,
                Result.SUCCESS,
            )
        self.assertEqual(
            self.controller.enqueue(self.request("living", Command.IDENTIFY)).result,
            Result.QUEUE_FULL,
        )
        self.assertEqual(self.controller.pending_count(), 8)

        self.controller = Controller(self.clock)
        self.controller.set_configured_motors(("living", "bedroom"))
        self.controller.enqueue(self.request("living", Command.IDENTIFY))
        active = self.controller.begin_next()
        self.assertEqual(active.result, Result.SUCCESS)
        for _ in range(7):
            self.assertEqual(
                self.controller.enqueue(self.request("bedroom", Command.IDENTIFY)).result,
                Result.SUCCESS,
            )
        self.assertEqual(
            self.controller.enqueue(self.request("bedroom", Command.IDENTIFY)).result,
            Result.QUEUE_FULL,
        )

    def test_expired_queue_entries_are_terminalized_and_capacity_is_reclaimed(self) -> None:
        queued = self.controller.enqueue(self.request("living", Command.IDENTIFY))
        self.clock.now = COMMAND_TTL_SECONDS
        fresh = self.controller.enqueue(self.request("living", Command.IDENTIFY))
        self.assertEqual(fresh.result, Result.BUSY)
        self.assertEqual(self.controller.pending_count(), 0)
        self.assertEqual(
            self.controller.drain_deferred_results(),
            (CompletionResult(Result.EXPIRED, queued.command_id),),
        )
        self.assertEqual(
            self.controller.enqueue(self.request("living", Command.IDENTIFY)).result,
            Result.SUCCESS,
        )

    def test_stop_preempts_same_motor_pending_work_and_is_selected_first(self) -> None:
        jog = self.controller.enqueue(self.request("living", Command.JOG))
        identify = self.controller.enqueue(self.request("living", Command.IDENTIFY))
        other = self.controller.enqueue(self.request("bedroom", Command.IDENTIFY))
        stop = self.controller.enqueue(self.request("living", Command.STOP))
        self.assertEqual(self.controller.pending_count(), 2)
        started = self.controller.begin_next()
        self.assertEqual(started.command_id, stop.command_id)
        self.assertEqual(
            self.controller.drain_deferred_results(),
            (
                CompletionResult(Result.PREEMPTED, jog.command_id),
                CompletionResult(Result.PREEMPTED, identify.command_id),
            ),
        )
        self.assertEqual(self.controller.complete(stop.command_id, Result.SUCCESS).result, Result.SUCCESS)
        self.assertEqual(self.controller.begin_next().command_id, other.command_id)

    def test_full_queue_stop_preempts_same_motor_and_preserves_other_motor_work(self) -> None:
        living_jog = self.controller.enqueue(self.request("living", Command.JOG))
        living_identify = self.controller.enqueue(self.request("living", Command.IDENTIFY))
        other = [
            self.controller.enqueue(self.request("bedroom", Command.IDENTIFY))
            for _ in range(MAX_QUEUE_DEPTH - 2)
        ]
        stop = self.controller.enqueue(self.request("living", Command.STOP))
        self.assertEqual(stop.result, Result.SUCCESS)
        self.assertEqual(self.controller.pending_count(), MAX_QUEUE_DEPTH - 1)

        started = self.controller.begin_next()
        self.assertEqual(started.command_id, stop.command_id)
        self.assertEqual(
            self.controller.drain_deferred_results(),
            (
                CompletionResult(Result.PREEMPTED, living_jog.command_id),
                CompletionResult(Result.PREEMPTED, living_identify.command_id),
            ),
        )
        self.assertEqual(self.controller.complete(stop.command_id, Result.SUCCESS).result, Result.SUCCESS)
        remaining = []
        while self.controller.pending_count():
            next_command = self.controller.begin_next()
            self.assertEqual(next_command.result, Result.SUCCESS)
            assert next_command.command is not None
            remaining.append(next_command.command.id)
            self.controller.complete(next_command.command_id, Result.SUCCESS)
        self.assertEqual(remaining, [item.command_id for item in other])

    def test_full_queue_stop_never_preempts_active_write(self) -> None:
        active_request = self.controller.enqueue(self.request("living", Command.IDENTIFY))
        started = self.controller.begin_next()
        self.assertEqual(started.command_id, active_request.command_id)
        self.assertTrue(self.controller.mark_write_attempted(started.command_id))
        pending = [
            self.controller.enqueue(self.request("bedroom", Command.IDENTIFY))
            for _ in range(MAX_QUEUE_DEPTH - 1)
        ]
        stop = self.controller.enqueue(self.request("living", Command.STOP))
        self.assertEqual(stop.result, Result.SUCCESS)
        self.assertEqual(self.controller.active_command().id, active_request.command_id)  # type: ignore[union-attr]
        self.assertEqual(self.controller.drain_deferred_results(), (
            CompletionResult(Result.PREEMPTED, pending[0].command_id),
        ))
        self.assertEqual(
            self.controller.complete(active_request.command_id, Result.SUCCESS),
            CompletionResult(Result.SUCCESS, active_request.command_id),
        )
        self.assertEqual(self.controller.begin_next().command_id, stop.command_id)
        self.controller.complete(stop.command_id, Result.SUCCESS)

    def test_full_queue_stop_evicts_jog_when_no_same_motor_work_exists(self) -> None:
        jog = self.controller.enqueue(self.request("bedroom", Command.JOG))
        preserved = [
            self.controller.enqueue(self.request("bedroom", Command.IDENTIFY))
            for _ in range(MAX_QUEUE_DEPTH - 1)
        ]
        stop = self.controller.enqueue(self.request("living", Command.STOP))
        self.assertEqual(stop.result, Result.SUCCESS)
        self.assertEqual(self.controller.begin_next().command_id, stop.command_id)
        self.assertEqual(
            self.controller.drain_deferred_results(),
            (CompletionResult(Result.PREEMPTED, jog.command_id),),
        )
        self.controller.complete(stop.command_id, Result.SUCCESS)
        remaining = []
        while self.controller.pending_count():
            started = self.controller.begin_next()
            self.assertEqual(started.result, Result.SUCCESS)
            remaining.append(started.command_id)
            self.controller.complete(started.command_id, Result.SUCCESS)
        self.assertEqual(remaining, [item.command_id for item in preserved])

    def test_full_queue_stop_evicts_a_non_stop_when_no_jog_exists(self) -> None:
        evicted = self.controller.enqueue(self.request("bedroom", Command.IDENTIFY))
        preserved = [
            self.controller.enqueue(self.request("bedroom", Command.IDENTIFY))
            for _ in range(MAX_QUEUE_DEPTH - 1)
        ]
        stop = self.controller.enqueue(self.request("living", Command.STOP))
        self.assertEqual(stop.result, Result.SUCCESS)
        self.assertEqual(self.controller.begin_next().command_id, stop.command_id)
        self.assertEqual(
            self.controller.drain_deferred_results(),
            (CompletionResult(Result.PREEMPTED, evicted.command_id),),
        )
        self.controller.complete(stop.command_id, Result.SUCCESS)
        remaining = []
        while self.controller.pending_count():
            started = self.controller.begin_next()
            self.assertEqual(started.result, Result.SUCCESS)
            remaining.append(started.command_id)
            self.controller.complete(started.command_id, Result.SUCCESS)
        self.assertEqual(remaining, [item.command_id for item in preserved])

    def test_deferred_backpressure_is_lossless_and_draining_restores_admission(self) -> None:
        queued = [
            self.controller.enqueue(self.request("living", Command.IDENTIFY))
            for _ in range(7)
        ]
        stop = self.controller.enqueue(self.request("living", Command.STOP))
        self.assertEqual(stop.result, Result.SUCCESS)
        self.assertEqual(self.controller.pending_count(), 1)
        self.assertEqual(
            self.controller.enqueue(self.request("bedroom", Command.IDENTIFY)).result,
            Result.BUSY,
        )
        deferred = self.controller.drain_deferred_results()
        self.assertEqual(
            deferred,
            tuple(CompletionResult(Result.PREEMPTED, item.command_id) for item in queued),
        )
        self.assertEqual(
            self.controller.enqueue(self.request("bedroom", Command.IDENTIFY)).result,
            Result.SUCCESS,
        )

    def test_stop_lifecycle_before_deferred_drain_is_bounded_and_lossless(self) -> None:
        living = [
            self.controller.enqueue(self.request("living", Command.IDENTIFY))
            for _ in range(2)
        ]
        other = [
            self.controller.enqueue(self.request("bedroom", Command.IDENTIFY))
            for _ in range(MAX_QUEUE_DEPTH - len(living))
        ]
        stop = self.controller.enqueue(self.request("living", Command.STOP))
        self.assertEqual(stop.result, Result.SUCCESS)

        self.controller.on_disconnect("living")
        self.controller.on_config_changed()
        deferred = self.controller.drain_deferred_results()

        self.assertLessEqual(len(deferred), MAX_TRACKED_RECORDS)
        self.assertEqual(
            {item.command_id for item in deferred},
            {
                *(item.command_id for item in living),
                *(item.command_id for item in other),
                stop.command_id,
            },
        )
        self.assertEqual(len(deferred), MAX_QUEUE_DEPTH + 1)
        self.assertEqual(
            sum(item.result is Result.PREEMPTED for item in deferred),
            len(living),
        )
        self.assertEqual(
            self.controller.enqueue(self.request("bedroom", Command.IDENTIFY)).result,
            Result.SUCCESS,
        )

    def test_repeated_lifecycle_events_do_not_duplicate_or_drop_terminal_results(self) -> None:
        queued = [
            self.controller.enqueue(self.request("living", Command.IDENTIFY))
            for _ in range(8)
        ]
        self.controller.on_config_changed()
        self.controller.on_config_changed()
        self.controller.on_unload()
        deferred = self.controller.drain_deferred_results()
        self.assertEqual(
            deferred,
            tuple(CompletionResult(Result.ERROR, item.command_id) for item in queued),
        )
        self.assertEqual(
            self.controller.enqueue(self.request("living", Command.IDENTIFY)).result,
            Result.SUCCESS,
        )


    def test_mark_write_attempted_rechecks_boundary_and_allows_fresh_direct_mark(self) -> None:
        queued = self.controller.enqueue(self.request("living", Command.JOG))
        started = self.controller.begin_next()
        self.clock.now = COMMAND_TTL_SECONDS
        self.assertFalse(self.controller.mark_write_attempted(started.command_id))
        self.assertFalse(self.controller.has_active())
        self.assertEqual(
            self.controller.complete(queued.command_id, Result.SUCCESS),
            CompletionResult(Result.NOT_FOUND, queued.command_id),
        )

        self.clock.now = 0.0
        fresh = self.controller.enqueue(self.request("living", Command.JOG))
        active = self.controller.begin_next()
        self.assertEqual(active.command_id, fresh.command_id)
        self.assertTrue(self.controller.mark_write_attempted(fresh.command_id))
        self.assertTrue(self.controller.has_active())

    def test_clock_faults_fail_closed_without_losing_admitted_work(self) -> None:
        for bad_value in (float("nan"), float("inf"), "invalid", RuntimeError("clock")):

            with self.subTest(operation="enqueue", bad_value=bad_value):
                clock = FaultClock(0.0)
                controller = Controller(clock)  # type: ignore[arg-type]
                controller.set_configured_motors(("living",))
                queued = controller.enqueue(self.request("living", Command.IDENTIFY))
                clock.value = bad_value
                rejected = controller.enqueue(self.request("living", Command.IDENTIFY))
                self.assertEqual(rejected.result, Result.ERROR)
                self.assertEqual(controller.pending_count(), 1)
                clock.value = 1.0
                self.assertEqual(controller.begin_next().command_id, queued.command_id)

    def test_clock_faults_terminalize_begin_and_prepare_and_preserve_deferred(self) -> None:
        clock = FaultClock(0.0)
        controller = Controller(clock)  # type: ignore[arg-type]
        controller.set_configured_motors(("living",))
        queued = controller.enqueue(self.request("living", Command.IDENTIFY))
        stop = controller.enqueue(self.request("living", Command.STOP))

        clock.value = ValueError("clock")
        failed_begin = controller.begin_next()
        self.assertEqual(failed_begin, BeginResult(Result.ERROR, stop.command_id))
        self.assertEqual(
            controller.drain_deferred_results(),
            (CompletionResult(Result.PREEMPTED, queued.command_id),),
        )

        clock.value = 0.0
        active_request = controller.enqueue(self.request("living", Command.IDENTIFY))
        active = controller.begin_next()
        self.assertEqual(active.command_id, active_request.command_id)
        clock.value = float("nan")
        self.assertEqual(controller.prepare_write(active.command_id), Result.ERROR)
        self.assertFalse(controller.has_active())

        clock.value = 0.0
        dangerous = controller.enqueue(self.request("living", Command.JOG))
        dangerous_active = controller.begin_next()
        clock.value = RuntimeError("clock")
        self.assertFalse(controller.mark_write_attempted(dangerous.command_id))
        self.assertFalse(controller.has_active())
        clock.value = 1.0
        self.assertEqual(dangerous_active.command_id, dangerous.command_id)

    def test_pre_write_timeout_finishes_without_replay_and_allows_new_human_attempt(self) -> None:
        queued = self.controller.enqueue(self.request("living", Command.JOG))
        started = self.controller.begin_next()
        finished = self.controller.complete(started.command_id, Result.TIMEOUT_BEFORE_WRITE)
        self.assertEqual(finished, CompletionResult(Result.TIMEOUT_BEFORE_WRITE, queued.command_id))
        self.assertFalse(self.controller.has_active())
        self.assertEqual(self.controller.enqueue(self.request("living", Command.JOG)).result, Result.SUCCESS)

    def test_post_write_timeout_is_unknown_and_cannot_replay_or_failover(self) -> None:
        queued = self.controller.enqueue(self.request("living", Command.JOG))
        started = self.controller.begin_next()
        self.assertTrue(self.controller.mark_write_attempted(started.command_id))
        self.assertEqual(
            self.controller.complete(queued.command_id, Result.TIMEOUT_BEFORE_WRITE),
            CompletionResult(Result.UNKNOWN_AFTER_WRITE, queued.command_id),
        )
        self.assertFalse(self.controller.has_active())
        self.assertEqual(self.controller.pending_count(), 0)

    def test_complete_rejects_non_result_inputs_without_emitting_them(self) -> None:
        queued = self.controller.enqueue(self.request("living", Command.IDENTIFY))
        started = self.controller.begin_next()
        invalid = self.controller.complete(started.command_id, "success")  # type: ignore[arg-type]
        self.assertEqual(invalid, CompletionResult(Result.INVALID_PAYLOAD, queued.command_id))
        self.assertTrue(self.controller.has_active())
        self.assertEqual(
            self.controller.complete(started.command_id, Result.SUCCESS),
            CompletionResult(Result.SUCCESS, queued.command_id),
        )

    def test_stale_completion_cannot_finish_newer_active_command(self) -> None:
        first = self.controller.enqueue(self.request("living", Command.IDENTIFY))
        self.controller.begin_next()
        self.controller.complete(first.command_id, Result.SUCCESS)
        second = self.controller.enqueue(self.request("bedroom", Command.IDENTIFY))
        self.controller.begin_next()
        self.assertEqual(
            self.controller.complete(first.command_id, Result.TIMEOUT_BEFORE_WRITE),
            CompletionResult(Result.STALE_CALLBACK, first.command_id),
        )
        self.assertTrue(self.controller.has_active())
        self.assertEqual(self.controller.active_command().id, second.command_id)  # type: ignore[union-attr]

    def test_reboot_preserves_command_ids_against_stale_callbacks(self) -> None:
        old = self.controller.enqueue(self.request("living", Command.IDENTIFY))
        self.assertEqual(old.command_id, 1)
        self.assertEqual(self.controller.begin_next().command_id, old.command_id)

        self.controller.on_reboot()
        self.assertEqual(
            self.controller.drain_deferred_results(),
            (CompletionResult(Result.ERROR, old.command_id),),
        )

        new = self.controller.enqueue(self.request("bedroom", Command.IDENTIFY))
        self.assertGreater(new.command_id, old.command_id)
        self.assertEqual(self.controller.begin_next().command_id, new.command_id)
        self.assertEqual(self.controller.prepare_write(old.command_id), Result.STALE_CALLBACK)
        self.assertFalse(self.controller.mark_write_attempted(old.command_id))
        self.assertEqual(
            self.controller.complete(old.command_id, Result.SUCCESS),
            CompletionResult(Result.STALE_CALLBACK, old.command_id),
        )
        self.assertTrue(self.controller.has_active())
        self.assertEqual(self.controller.active_command().id, new.command_id)  # type: ignore[union-attr]
        self.assertEqual(
            self.controller.complete(new.command_id, Result.SUCCESS),
            CompletionResult(Result.SUCCESS, new.command_id),
        )

    def test_maximal_lifecycle_cleanup_preserves_nine_unique_terminal_records(self) -> None:
        queued = [
            self.controller.enqueue(self.request("living", Command.IDENTIFY))
            for _ in range(MAX_QUEUE_DEPTH)
        ]
        stop = self.controller.enqueue(self.request("living", Command.STOP))
        self.assertTrue(all(item.result is Result.SUCCESS for item in queued))
        self.assertEqual(stop.result, Result.SUCCESS)
        self.assertEqual(self.controller.pending_count(), 1)

        self.controller.on_config_changed()
        blocked = self.controller.enqueue(self.request("bedroom", Command.IDENTIFY))
        self.assertEqual(blocked.result, Result.BUSY)

        deferred = self.controller.drain_deferred_results()
        self.assertEqual(len(deferred), MAX_TRACKED_RECORDS)
        self.assertEqual(len({item.command_id for item in deferred}), MAX_TRACKED_RECORDS)
        self.assertEqual(len(set(deferred)), MAX_TRACKED_RECORDS)
        self.assertEqual(
            {item.command_id for item in deferred},
            {item.command_id for item in queued} | {stop.command_id},
        )
        self.assertEqual(
            sum(item.result is Result.PREEMPTED for item in deferred),
            MAX_QUEUE_DEPTH,
        )
        self.assertEqual(
            sum(item.result is Result.ERROR for item in deferred),
            1,
        )
        self.assertEqual(
            deferred[:MAX_QUEUE_DEPTH],
            tuple(CompletionResult(Result.PREEMPTED, item.command_id) for item in queued),
        )
        self.assertEqual(deferred[-1], CompletionResult(Result.ERROR, stop.command_id))
        self.assertEqual(
            self.controller.enqueue(self.request("bedroom", Command.IDENTIFY)).result,
            Result.SUCCESS,
        )


    def test_prepare_expiry_resets_active_and_stale_callbacks_cannot_mutate_new_work(self) -> None:
        first = self.controller.enqueue(self.request("living", Command.IDENTIFY))
        started = self.controller.begin_next()
        self.clock.now = COMMAND_TTL_SECONDS
        self.assertEqual(self.controller.prepare_write(started.command_id), Result.EXPIRED)
        self.assertFalse(self.controller.has_active())
        self.assertEqual(self.controller.prepare_write(started.command_id), Result.NOT_FOUND)

        second = self.controller.enqueue(self.request("bedroom", Command.IDENTIFY))
        active = self.controller.begin_next()
        self.assertEqual(active.command_id, second.command_id)
        self.assertEqual(self.controller.prepare_write(first.command_id), Result.STALE_CALLBACK)
        self.assertFalse(self.controller.mark_write_attempted(first.command_id))
        self.assertEqual(
            self.controller.complete(first.command_id, Result.SUCCESS),
            CompletionResult(Result.STALE_CALLBACK, first.command_id),
        )
        self.assertTrue(self.controller.has_active())
        self.assertEqual(
            self.controller.complete(second.command_id, Result.SUCCESS),
            CompletionResult(Result.SUCCESS, second.command_id),
        )

    def test_disconnect_clears_pending_and_active_ownership_for_only_that_motor(self) -> None:
        living = self.controller.enqueue(self.request("living", Command.IDENTIFY))
        bedroom = self.controller.enqueue(self.request("bedroom", Command.IDENTIFY))
        started = self.controller.begin_next()
        self.assertEqual(started.command_id, living.command_id)
        self.controller.on_disconnect("living")
        self.assertFalse(self.controller.has_active())
        self.assertEqual(self.controller.pending_count(), 1)
        self.assertEqual(self.controller.begin_next().command_id, bedroom.command_id)

    def test_result_values_are_machine_readable(self) -> None:
        self.assertEqual(Result.QUEUE_FULL.value, "queue_full")
        self.assertEqual(Result.EXPIRED.value, "expired")
        self.assertEqual(Result.AUTH_FAILED.value, "auth_failed")
        self.assertEqual(Result.DISCONNECTED.value, "disconnected")
        self.assertEqual(Result.ERROR.value, "error")
        self.assertEqual(result_name(Result.SUCCESS), "success")

    def test_config_removal_clears_motor_registry_and_ownership(self) -> None:
        queued = self.controller.enqueue(self.request("living", Command.IDENTIFY))
        self.controller.on_config_removed()
        self.assertFalse(self.controller.is_configured("living"))
        self.assertFalse(self.controller.has_active())
        self.assertEqual(self.controller.pending_count(), 0)
        self.assertIn(CompletionResult(Result.ERROR, queued.command_id), self.controller.drain_deferred_results())

    def test_disconnect_after_write_is_unknown_after_write(self) -> None:
        self.controller.enqueue(self.request("living", Command.IDENTIFY))
        started = self.controller.begin_next()
        self.assertTrue(self.controller.mark_write_attempted(started.command_id))
        self.controller.on_disconnect("living")
        self.assertEqual(
            self.controller.drain_deferred_results(),
            (CompletionResult(Result.UNKNOWN_AFTER_WRITE, started.command_id),),
        )

    def test_global_lifecycle_after_write_is_unknown_after_write(self) -> None:
        for lifecycle_name in ("on_config_changed", "on_unload"):
            controller = Controller(self.clock)
            controller.set_configured_motors(("living", "bedroom"))
            controller.enqueue(self.request("living", Command.IDENTIFY))
            started = controller.begin_next()
            self.assertTrue(controller.mark_write_attempted(started.command_id))
            getattr(controller, lifecycle_name)()
            self.assertEqual(
                controller.drain_deferred_results(),
                (CompletionResult(Result.UNKNOWN_AFTER_WRITE, started.command_id),),
            )

    def test_motor_registry_rejects_duplicates_and_malformed_ids_without_partial_reset(self) -> None:
        for motor_ids in (("living", "living"), ("living", 4), ("", "bedroom")):
            with self.assertRaises(ValueError):
                self.controller.set_configured_motors(motor_ids)  # type: ignore[arg-type]
            self.assertTrue(self.controller.is_configured("living"))


if __name__ == "__main__":
    unittest.main()
