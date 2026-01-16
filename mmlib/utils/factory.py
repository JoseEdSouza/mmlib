from typing import Callable, Any
from functools import wraps


def factory[**P, R](
    cls: Callable[P, Any],
) -> Callable[[Callable[..., R]], Callable[P, R]]:
    """
    Decorator that forwards the parameter signature of a source class/callable (cls)
    to the decorated function.

    Usage:
        @factory(MyClass)
        def my_factory(*args, **kwargs) -> MyInterface:
            return MyClass(*args, **kwargs)

    The resulting my_factory will have the same type signature as MyClass.__init__.
    """

    @wraps(cls.__init__)
    def decorator(func: Callable[..., R]) -> Callable[P, R]:
        return func  # type: ignore

    return decorator
