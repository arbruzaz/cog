import io
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import signal
import sys
import traceback
import time
import types
import contextlib

from pydantic import ValidationError
import redis
import requests

from ..predictor import BasePredictor, get_input_type, load_predictor
from ..json import encode_json
from ..response import Status
from .runner import PredictionRunner


class timeout:
    """A context manager that times out after a given number of seconds."""

    def __init__(
        self,
        seconds: Optional[int],
        elapsed: Optional[int] = None,
        error_message: str = "Prediction timed out",
    ) -> None:
        if elapsed is None or seconds is None:
            self.seconds = seconds
        else:
            self.seconds = seconds - int(elapsed)
        self.error_message = error_message

    def handle_timeout(self, signum: Any, frame: Any) -> None:
        raise TimeoutError(self.error_message)

    def __enter__(self) -> None:
        if self.seconds is not None:
            if self.seconds <= 0:
                self.handle_timeout(None, None)
            else:
                signal.signal(signal.SIGALRM, self.handle_timeout)
                signal.alarm(self.seconds)

    def __exit__(self, type: Any, value: Any, traceback: Any) -> None:
        if self.seconds is not None:
            signal.alarm(0)


class RedisQueueWorker:
    SETUP_TIME_QUEUE_SUFFIX = "-setup-time"
    RUN_TIME_QUEUE_SUFFIX = "-run-time"
    STAGE_SETUP = "setup"
    STAGE_RUN = "run"

    def __init__(
        self,
        predictor: BasePredictor,
        redis_host: str,
        redis_port: int,
        input_queue: str,
        upload_url: str,
        consumer_id: str,
        model_id: Optional[str] = None,
        log_queue: Optional[str] = None,
        predict_timeout: Optional[int] = None,
        redis_db: int = 0,
    ):
        self.runner = PredictionRunner()
        self.redis_host = redis_host
        self.redis_port = redis_port
        self.input_queue = input_queue
        self.upload_url = upload_url
        self.consumer_id = consumer_id
        self.model_id = model_id
        self.log_queue = log_queue
        self.predict_timeout = predict_timeout
        self.redis_db = redis_db
        # TODO: respect max_processing_time in message handling
        self.max_processing_time = 10 * 60  # timeout after 10 minutes

        # Set up types
        self.InputType = get_input_type(predictor)

        self.redis = redis.Redis(
            host=self.redis_host, port=self.redis_port, db=self.redis_db
        )
        self.should_exit = False
        self.setup_time_queue = input_queue + self.SETUP_TIME_QUEUE_SUFFIX
        self.predict_time_queue = input_queue + self.RUN_TIME_QUEUE_SUFFIX
        self.stats_queue_length = 100

        sys.stderr.write(
            f"Connected to Redis: {self.redis_host}:{self.redis_port} (db {self.redis_db})\n"
        )

    def signal_exit(self, signum: Any, frame: Any) -> None:
        self.should_exit = True
        sys.stderr.write("Caught SIGTERM, exiting...\n")

    def receive_message(self) -> Tuple[Optional[str], Optional[str]]:
        # first, try to autoclaim old messages from pending queue
        raw_messages = self.redis.execute_command(
            "XAUTOCLAIM",
            self.input_queue,
            self.input_queue,
            self.consumer_id,
            str(self.max_processing_time * 1000),
            "0-0",
            "COUNT",
            1,
        )
        # format: [[b'1619393873567-0', [b'mykey', b'myval']]]
        if raw_messages and raw_messages[0] is not None:
            key, raw_message = raw_messages[0]
            assert raw_message[0] == b"value"
            return key.decode(), raw_message[1].decode()

        # if no old messages exist, get message from main queue
        raw_messages = self.redis.xreadgroup(
            groupname=self.input_queue,
            consumername=self.consumer_id,
            streams={self.input_queue: ">"},
            count=1,
            block=1000,
        )
        if not raw_messages:
            return None, None

        # format: [[b'mystream', [(b'1619395583065-0', {b'mykey': b'myval6'})]]]
        key, raw_message = raw_messages[0][1][0]
        return key.decode(), raw_message[b"value"].decode()

    def start(self) -> None:
        signal.signal(signal.SIGTERM, self.signal_exit)
        start_time = time.time()

        # TODO(bfirsh): setup should time out too, but we don't display these logs to the user, so don't timeout to avoid confusion
        self.runner.setup()

        setup_time = time.time() - start_time
        self.redis.xadd(
            self.setup_time_queue,
            fields={"duration": setup_time},
            maxlen=self.stats_queue_length,
        )
        sys.stderr.write(f"Setup time: {setup_time:.2f}\n")

        sys.stderr.write(f"Waiting for message on {self.input_queue}\n")
        while not self.should_exit:
            try:
                message_id, message_json = self.receive_message()
                if message_json is None:
                    # tight loop in order to respect self.should_exit
                    continue

                message = json.loads(message_json)
                response_queue = message["response_queue"]
                sys.stderr.write(f"Received message on {self.input_queue}\n")
                cleanup_functions: List[Callable] = []
                try:
                    start_time = time.time()
                    self.handle_message(response_queue, message, cleanup_functions)
                    self.redis.xack(self.input_queue, self.input_queue, message_id)
                    self.redis.xdel(
                        self.input_queue, message_id
                    )  # xdel to be able to get stream size
                    run_time = time.time() - start_time
                    self.redis.xadd(
                        self.predict_time_queue,
                        fields={"duration": run_time},
                        maxlen=self.stats_queue_length,
                    )
                    sys.stderr.write(f"Run time: {run_time:.2f}\n")
                except Exception as e:
                    self.push_error(response_queue, e)
                    self.redis.xack(self.input_queue, self.input_queue, message_id)
                    self.redis.xdel(self.input_queue, message_id)
                finally:
                    for cleanup_function in cleanup_functions:
                        try:
                            cleanup_function()
                        except Exception as e:
                            sys.stderr.write(f"Cleanup function caught error: {e}")
            except Exception as e:
                tb = traceback.format_exc()
                sys.stderr.write(f"Failed to handle message: {tb}\n")

    def handle_message(
        self,
        response_queue: str,
        message: Dict[str, Any],
        cleanup_functions: List[Callable],
    ) -> None:
        try:
            input_obj = self.InputType(**message["input"])
        except ValidationError as e:
            tb = traceback.format_exc()
            sys.stderr.write(tb)
            self.push_error(response_queue, e)
            return

        cleanup_functions.append(input_obj.cleanup)

        start_time = time.time()

        with timeout(seconds=self.predict_timeout):
            self.runner.run(**input_obj.dict())

            logs: List[str] = []
            response = {
                "status": Status.PROCESSING,
                "output": None,
                "logs": logs,
            }

            # just send logs until output starts
            while self.runner.is_processing() and not self.runner.has_output_waiting():
                if self.runner.has_logs_waiting():
                    logs.extend(self.runner.read_logs())
                    self.redis.rpush(response_queue, json.dumps(response))

            if self.runner.error() is not None:
                response["status"] = Status.FAILED
                response["error"] = str(self.runner.error())  # type: ignore
                self.redis.rpush(response_queue, json.dumps(response))
                return

            if self.runner.is_output_generator():
                output = response["output"] = []

                while self.runner.is_processing():
                    if (
                        self.runner.has_output_waiting()
                        or self.runner.has_logs_waiting()
                    ):
                        new_output = [
                            self.encode_json(o) for o in self.runner.read_output()
                        ]
                        new_logs = self.runner.read_logs()

                        # sometimes it'll say there's output when there's none
                        if new_output == [] and new_logs == []:
                            continue

                        output.extend(new_output)
                        logs.extend(new_logs)

                        # we could `time.sleep(0.1)` and check `is_processing()`
                        # here to give the predictor subprocess a chance to exit
                        # so we don't send a double message for final output, at
                        # the cost of extra latency
                        self.redis.rpush(response_queue, json.dumps(response))

                if self.runner.error() is not None:
                    response["status"] = Status.FAILED
                    response["error"] = str(self.runner.error())  # type: ignore
                    self.redis.rpush(response_queue, json.dumps(response))
                    return

                response["status"] = Status.SUCCEEDED
                output.extend(self.encode_json(o) for o in self.runner.read_output())
                logs.extend(self.runner.read_logs())
                self.redis.rpush(response_queue, json.dumps(response))

            else:
                # just send logs until output ends
                while self.runner.is_processing():
                    if self.runner.has_logs_waiting():
                        logs.extend(self.runner.read_logs())
                        self.redis.rpush(response_queue, json.dumps(response))

                if self.runner.error() is not None:
                    response["status"] = Status.FAILED
                    response["error"] = str(self.runner.error())  # type: ignore
                    self.redis.rpush(response_queue, json.dumps(response))
                    return

                output = self.runner.read_output()
                assert len(output) == 1

                response["status"] = Status.SUCCEEDED
                response["output"] = self.encode_json(output[0])
                logs.extend(self.runner.read_logs())
                self.redis.rpush(response_queue, json.dumps(response))

    def download(self, url: str) -> bytes:
        resp = requests.get(url)
        resp.raise_for_status()
        return resp.content

    def push_error(self, response_queue: str, error: Any) -> None:
        message = json.dumps(
            {
                "status": "failed",
                "error": str(error),
            }
        )
        sys.stderr.write(f"Pushing error to {response_queue}\n")
        self.redis.rpush(response_queue, message)

    def encode_json(self, obj: Any) -> Any:
        def upload_file(fh: io.IOBase) -> str:
            resp = requests.put(self.upload_url, files={"file": fh})
            resp.raise_for_status()
            return resp.json()["url"]

        return encode_json(obj, upload_file)


def _queue_worker_from_argv(
    predictor: BasePredictor,
    redis_host: str,
    redis_port: str,
    input_queue: str,
    upload_url: str,
    comsumer_id: str,
    model_id: str,
    log_queue: str,
    predict_timeout: Optional[str] = None,
) -> RedisQueueWorker:
    """
    Construct a RedisQueueWorker object from sys.argv, taking into account optional arguments and types.

    This is intensely fragile. This should be kwargs or JSON or something like that.
    """
    if predict_timeout is not None:
        predict_timeout_int = int(predict_timeout)
    else:
        predict_timeout_int = None
    return RedisQueueWorker(
        predictor,
        redis_host,
        int(redis_port),
        input_queue,
        upload_url,
        comsumer_id,
        model_id,
        log_queue,
        predict_timeout_int,
    )


if __name__ == "__main__":
    predictor = load_predictor()

    worker = _queue_worker_from_argv(predictor, *sys.argv[1:])
    worker.start()
