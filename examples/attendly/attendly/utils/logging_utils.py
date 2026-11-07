import logging
import time
import functools
import inspect
import uuid
from typing import Any, Callable, Optional, Tuple, Dict
import contextvars

# Context var for correlating logs per request/task
request_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("request_id", default=None)


def set_request_id(req_id: Optional[str]) -> None:
	request_id_var.set(req_id)


def get_request_id() -> Optional[str]:
	return request_id_var.get()


def new_request_id() -> str:
	return uuid.uuid4().hex


def _redact(value: Any, key: Optional[str] = None) -> Any:
	if key and key.lower() in {"password", "secret", "token", "authorization", "api_key", "apikey"}:
		return "***redacted***"
	# Avoid huge dumps
	try:
		text = str(value)
		if len(text) > 256:
			return text[:256] + "...[truncated]"
		return value
	except Exception:
		return "[unrepr]"


def _format_args(args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> str:
	parts = []
	for i, a in enumerate(args):
		parts.append(_redact(a))
	for k, v in kwargs.items():
		parts.append(f"{k}={_redact(v, k)}")
	return ", ".join(map(lambda x: str(x), parts))


def log_call(logger: Optional[logging.Logger] = None, level: int = logging.DEBUG, include_result: bool = False) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
	"""Decorator to log function entry/exit, duration and exceptions.

	- Works with sync and async callables
	- Redacts common sensitive fields
	- Includes request_id when available
	"""
	def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
		log = logger or logging.getLogger(func.__module__)
		is_coroutine = inspect.iscoroutinefunction(func)

		@functools.wraps(func)
		def wrapper(*args: Any, **kwargs: Any):
			req_id = get_request_id()
			args_str = _format_args(args, kwargs)
			prefix = f"[req_id={req_id}] " if req_id else ""
			log.log(level, f"{prefix}ENTER {func.__qualname__}({args_str})")
			t0 = time.perf_counter()
			try:
				result = func(*args, **kwargs)
				if inspect.isawaitable(result):
					# If the wrapped sync wrapper returns awaitable (unlikely), handle here
					async def _await_and_log():
						try:
							res = await result  # type: ignore
							elapsed_ms = int((time.perf_counter() - t0) * 1000)
							if include_result:
								log.log(level, f"{prefix}EXIT {func.__qualname__} -> { _redact(res) } in {elapsed_ms}ms")
							else:
								log.log(level, f"{prefix}EXIT {func.__qualname__} in {elapsed_ms}ms")
							return res
						except Exception as e:
							elapsed_ms = int((time.perf_counter() - t0) * 1000)
							log.exception(f"{prefix}ERROR {func.__qualname__} after {elapsed_ms}ms: {e}")
							raise
					return _await_and_log()
				elapsed_ms = int((time.perf_counter() - t0) * 1000)
				if include_result:
					log.log(level, f"{prefix}EXIT {func.__qualname__} -> { _redact(result) } in {elapsed_ms}ms")
				else:
					log.log(level, f"{prefix}EXIT {func.__qualname__} in {elapsed_ms}ms")
				return result
			except Exception as e:
				elapsed_ms = int((time.perf_counter() - t0) * 1000)
				log.exception(f"{prefix}ERROR {func.__qualname__} after {elapsed_ms}ms: {e}")
				raise

		if is_coroutine:
			@functools.wraps(func)
			async def async_wrapper(*args: Any, **kwargs: Any):
				req_id = get_request_id()
				args_str = _format_args(args, kwargs)
				prefix = f"[req_id={req_id}] " if req_id else ""
				log.log(level, f"{prefix}ENTER {func.__qualname__}({args_str})")
				t0 = time.perf_counter()
				try:
					res = await func(*args, **kwargs)
					elapsed_ms = int((time.perf_counter() - t0) * 1000)
					if include_result:
						log.log(level, f"{prefix}EXIT {func.__qualname__} -> { _redact(res) } in {elapsed_ms}ms")
					else:
						log.log(level, f"{prefix}EXIT {func.__qualname__} in {elapsed_ms}ms")
					return res
				except Exception as e:
					elapsed_ms = int((time.perf_counter() - t0) * 1000)
					log.exception(f"{prefix}ERROR {func.__qualname__} after {elapsed_ms}ms: {e}")
					raise
			return async_wrapper

		return wrapper

	return decorator
