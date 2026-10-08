"""Declare RabbitMQ topology from infra/rabbitmq/definitions.json.

In docker compose the same file is loaded by the rabbitmq-init service; tests use this
module to get an identical topology in a throwaway broker (optionally with short TTLs).
"""

import json
from pathlib import Path
from typing import Any

from faststream.rabbit import ExchangeType, QueueType, RabbitBroker, RabbitExchange, RabbitQueue

DEFINITIONS_PATH = Path(__file__).resolve().parents[4] / "infra" / "rabbitmq" / "definitions.json"


def load_definitions(path: Path = DEFINITIONS_PATH) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def queue_names(definitions: dict[str, Any]) -> list[str]:
    return [queue["name"] for queue in definitions["queues"]]


async def declare_topology(broker: RabbitBroker, definitions: dict[str, Any]) -> None:
    for exchange in definitions["exchanges"]:
        await broker.declare_exchange(
            RabbitExchange(
                exchange["name"],
                type=ExchangeType(exchange["type"]),
                durable=exchange["durable"],
            )
        )

    queues = {}
    for queue in definitions["queues"]:
        arguments: Any = dict(queue["arguments"])
        if arguments.pop("x-queue-type", "classic") == QueueType.QUORUM.value:
            declared = RabbitQueue(
                queue["name"], queue_type=QueueType.QUORUM, durable=True, arguments=arguments
            )
        else:
            declared = RabbitQueue(queue["name"], durable=True, arguments=arguments)
        queues[queue["name"]] = await broker.declare_queue(declared)

    for binding in definitions["bindings"]:
        await queues[binding["destination"]].bind(
            binding["source"], routing_key=binding["routing_key"]
        )
