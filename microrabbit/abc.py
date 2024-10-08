import asyncio
import importlib.util
import json
import uuid
from functools import partial
from pathlib import Path
from typing import Awaitable, Callable, MutableMapping, Any, Dict, Tuple

import aio_pika
from aio_pika import Channel, Connection, Exchange, Queue, IncomingMessage

from .logger import get_logger
from .types import QueueOptions, ConsumerOptions

_logger = get_logger(__name__)
_queues: Dict[str, Tuple[Callable[..., Awaitable[Any]], QueueOptions, ConsumerOptions]] = {}


class Singleton(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(Singleton, cls).__call__(*args, **kwargs)
        return cls._instances[cls]


class PluginLoader:
    def __init__(self, plugins_dir: Path):
        self.plugins_dir = plugins_dir

    def load_plugins(self):
        for file in self.plugins_dir.iterdir():
            if file.suffix == ".py":
                spec = importlib.util.spec_from_file_location(file.stem, file)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)


def _is_serializable(obj):
    try:
        json.dumps(obj)
        return True
    except (TypeError, ValueError):
        return False


class AbstractClient(metaclass=Singleton):
    def __init__(self, host: str, plugins: str = None):
        """
        Constructor for the AbstractClient class singleton, which is used to interact with RabbitMQ, declare queues, and
        consume messages from them.
        :param host:  The RabbitMQ host to connect to
        :param plugins: The directory where the plugins are stored. This is used to dynamically import the plugins.
        """
        self.host = host
        self.plugins = plugins
        self._exchange = None
        self._channel: Channel = None
        self._connection: Connection = None
        self._on_ready_func: Callable[..., Awaitable] = None
        self._futures: MutableMapping[str, asyncio.Future] = {}
        self._callbacks: MutableMapping[str, Queue] = {}

        if plugins and plugins == ".":
            raise ValueError("Plugins directory cannot be the current directory")
        if plugins and not Path(plugins).exists():
            raise FileNotFoundError(f"Plugins directory {plugins} does not exist")

        if plugins and not plugins.isspace() and Path(plugins).exists() and Path(plugins).is_dir():
            plugin_loader = PluginLoader(Path(plugins))
            plugin_loader.load_plugins()

    async def connect(self):
        self._connection = await aio_pika.connect(self.host)
        self._channel = await self._connection.channel()
        self._exchange = self._channel.default_exchange
        return self._connection, self._channel

    async def close(self):
        await self._connection.close()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.close()
        return False

    async def is_connected(self) -> bool:
        """
        Check if the client is connected to RabbitMQ.
        """
        if self._connection is None or self._connection.is_closed:
            return False

        async def message_handler(exchange: Exchange, message: IncomingMessage):
            await self.publish(
                exchange=exchange,
                routing_key=message.reply_to,
                correlation_id=message.correlation_id,
                body=True
            )

        uid = str(uuid.uuid4())
        new_queue = await self.declare_queue(options=QueueOptions(exclusive=True, auto_delete=True))
        task = asyncio.create_task(
            new_queue.consume(
                partial(message_handler, self._exchange),
                no_ack=True,
                exclusive=True,
                timeout=1
            )
        )

        try:
            resp = await self.simple_publish(new_queue.name, {}, correlation_id=uid)
            return bool(resp)
        except Exception:
            return False

        finally:
            task.cancel()
            await self._channel.queue_delete(new_queue.name)

    async def declare_queue(self, queue_name: str = None, options: QueueOptions = QueueOptions()):
        return await self._channel.declare_queue(name=queue_name, **options.to_dict())

    @staticmethod
    def on_message(
            queue_name: str,
            queue_options: QueueOptions = QueueOptions(),
            consume_options: ConsumerOptions = ConsumerOptions()
    ):
        """
        Decorator to add a function to a queue. This function is called when a message is received in the queue.
        :param consume_options:
        :param queue_name: The name of the queue to add the function to.
        :param queue_options: The options to use when declaring the queue.
        ```python
        @client.on_message("queue_name")
        async def test(data: dict) -> dict:
            print(f"Received message {data}")
            return {} # Return a response to the message could be anything serializable
        ```
        """

        def decorator(func: Callable[..., Awaitable]) -> Callable[..., Awaitable]:
            if queue_name in _queues:
                raise ValueError(f"Function {queue_name} already added to function {_queues[queue_name][0].__name__}")

            _queues[queue_name] = (func, queue_options, consume_options)
            _logger.debug(f"Added function {func.__name__} to {queue_name} not yet consumed")

            return func

        return decorator

    def on_ready(self, func: Callable[..., Awaitable[None]]):
        """
        Decorator to set the on_ready function. This function is called when the client is ready to consume messages.
        :param func: The function to call when the client is ready to consume messages.
        ```python
        @client.on_ready
        async def on_ready():
            print("[*] Waiting for messages. To exit press CTRL+C")
        ```
        """
        self._on_ready_func = func
        return func

    async def _on_response(self, message: IncomingMessage) -> None:
        if message.correlation_id is None:
            _logger.error(f"Bad message {message!r}")
            return

        future: asyncio.Future = self._futures.pop(message.correlation_id)
        future.set_result(message.body)

    async def simple_publish(self, routing_key: str, body: Any, correlation_id=None, timeout: int = 10, decode=True):
        """
        Publish a message to the default exchange with a routing key and correlation id.
        :param routing_key: the routing key to use
        :param body: the body of the message
        :param correlation_id: the correlation id to use if not provided a new one will be generated
        :param timeout: the timeout to wait for the response
        :param decode: whether to decode the response
        :return:
        """

        if self._connection is None:
            raise RuntimeError("Client not connected to RabbitMQ, call connect() first")

        if correlation_id is None:
            correlation_id = str(uuid.uuid4())

        if not _is_serializable(body):
            raise ValueError("Body must be a serializable object")

        if routing_key is None:
            routing_key = str(uuid.uuid4())

        content_type = "application/json"
        if not isinstance(body, dict):
            content_type = "text/plain"

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        self._futures[correlation_id] = future
        self._callbacks[correlation_id] = await self._channel.declare_queue(exclusive=True, auto_delete=True)

        await self._callbacks[correlation_id].consume(self._on_response, no_ack=True, exclusive=True, timeout=timeout)

        await self._exchange.publish(
            message=aio_pika.Message(
                body=str(body).encode(),
                content_type=content_type,
                correlation_id=correlation_id,
                reply_to=self._callbacks[correlation_id].name
            ),
            routing_key=routing_key
        )

        try:
            response = await asyncio.wait_for(future, timeout=timeout)

            if decode:
                return response.decode()
            return response
        except asyncio.TimeoutError as e:
            raise TimeoutError("The request timed out") from e
        finally:
            await self._channel.queue_delete(self._callbacks[correlation_id].name)   
            del self._callbacks[correlation_id]

    @staticmethod
    async def publish(exchange: Exchange, routing_key: str, correlation_id, body: Dict):
        """
        Publish a message to an exchange with a routing key and correlation id.
        :param exchange: the exchange to publish the message to
        :param routing_key: the routing key to use
        :param correlation_id: the correlation id to use
        :param body: the body of the message
        :return:
        """
        return await exchange.publish(
            message=aio_pika.Message(
                body=str(body).encode(),
                correlation_id=correlation_id
            ),
            routing_key=routing_key
        )
