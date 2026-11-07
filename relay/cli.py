"""Run Relay from the terminal, no iPhone needed.

    python -m relay.cli "open settings and turn on dark mode"
    python -m relay.cli "open my calc notes" --device mac

It prints every spoken update, and when Relay asks a question
("About to tap Send. Should I do it?") you answer by typing.
"""
import argparse
import logging

from relay import agent
from relay.agent import Task


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one Relay task from the terminal.")
    parser.add_argument("goal", help='what you would say, e.g. "open settings"')
    parser.add_argument("--device", choices=["phone", "mac"], default=None)
    parser.add_argument("-v", "--verbose", action="store_true", help="show which brain picked each step")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="  [%(name)s] %(message)s")

    agent.REPLY_TIMEOUT = 600  # typing takes longer than talking: wait up to 10 min
    task = Task(args.goal, device_name=args.device).start()
    shown = 0
    while True:
        task.wait_for_change(timeout=5)
        # Print any new narration lines ("Opening Settings", "Tapping Display"...)
        for line in task.narration[shown:]:
            print(f"Relay: {line}")
        shown = len(task.narration)

        if task.status == "waiting":
            answer = input(f"Relay asks: {task.say()}\nYou: ")
            task.reply(answer)
        elif task.finished:
            print(f"\n[{task.status}] {task.say()}")
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
