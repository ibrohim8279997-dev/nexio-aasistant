"""Запуск агента: python run_agent.py <имя_агента>  (например: kimo, assistant, agent_builder)."""
import sys

from bot.runner import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
