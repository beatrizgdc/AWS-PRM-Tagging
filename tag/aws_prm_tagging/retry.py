"""Retry com backoff exponencial para chamadas de API resilientes a throttling."""
from __future__ import annotations

import logging
import random
import time
from functools import wraps
from typing import Callable, TypeVar

from botocore.exceptions import ClientError, EndpointConnectionError

logger = logging.getLogger(__name__)

_RETRYABLE_ERROR_CODES = {
    "Throttling",
    "ThrottlingException",
    "TooManyRequestsException",
    "RequestLimitExceeded",
    "ProvisionedThroughputExceededException",
    "RequestThrottledException",
    "SlowDown",
}

T = TypeVar("T")


def with_backoff(
    max_attempts: int = 6,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator: repete a chamada em caso de throttling, com backoff
    exponencial + jitter. Outros erros (ex.: AccessDenied) não são
    retentados — propagam imediatamente para o chamador decidir (logar e
    continuar, tipicamente)."""

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            attempt = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except ClientError as exc:
                    error_code = exc.response.get("Error", {}).get("Code", "")
                    attempt += 1
                    if error_code not in _RETRYABLE_ERROR_CODES or attempt >= max_attempts:
                        raise
                    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    delay += random.uniform(0, delay * 0.1)
                    logger.warning(
                        "Throttling em %s (tentativa %d/%d), aguardando %.1fs",
                        func.__name__,
                        attempt,
                        max_attempts,
                        delay,
                    )
                    time.sleep(delay)
                except EndpointConnectionError:
                    attempt += 1
                    if attempt >= max_attempts:
                        raise
                    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    logger.warning(
                        "Erro de conexão em %s (tentativa %d/%d), aguardando %.1fs",
                        func.__name__,
                        attempt,
                        max_attempts,
                        delay,
                    )
                    time.sleep(delay)

        return wrapper

    return decorator
