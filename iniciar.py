#!/usr/bin/env python3
"""Modo interativo da automação AWS PRM (MVP2).

    python3 iniciar.py            (Linux/macOS/WSL)
    py iniciar.py                 (Windows)

Conduz passo a passo: conta -> mapeamento -> simulação -> tagueamento ->
implantação da automação contínua, com resumo e confirmação em cada passo.
Opções: python3 iniciar.py --help
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aws_prm_tagging.assistente import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
