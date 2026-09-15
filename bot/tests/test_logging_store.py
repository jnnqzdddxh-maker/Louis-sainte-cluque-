import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.logging_store import DecisionLogger, _tail_lines


def test_read_all_round_trips_logged_events(tmp_path):
    logger = DecisionLogger(path=tmp_path / "decisions.jsonl", max_size_bytes=10**9)
    logger.log("engine_start", dry_run=True, capital_eur=500)
    logger.log("candidate_scored", token_address="ABC", chain="solana")

    events = logger.read_all()
    assert [e["event_type"] for e in events] == ["engine_start", "candidate_scored"]


def test_read_all_with_limit_returns_only_the_last_n_lines(tmp_path):
    logger = DecisionLogger(path=tmp_path / "decisions.jsonl", max_size_bytes=10**9)
    for i in range(10):
        logger.log("candidate_scored", token_address=f"TOKEN_{i}")

    events = logger.read_all(limit=3)
    assert [e["token_address"] for e in events] == ["TOKEN_7", "TOKEN_8", "TOKEN_9"]


def test_tail_lines_handles_chunk_boundaries(tmp_path):
    path = tmp_path / "big.jsonl"
    # Force plusieurs chunks (chunk_size = 1 Mo dans _tail_lines) pour
    # vérifier que la lecture en arrière ne coupe pas une ligne en deux.
    lines = [f'{{"n": {i}}}' for i in range(200_000)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    tail = _tail_lines(path, 5)
    assert [line.strip() for line in tail] == [f'{{"n": {i}}}' for i in range(199_995, 200_000)]


def test_log_rotates_when_file_exceeds_max_size(tmp_path):
    """Bug réel corrigé le 15/09/2026 : decisions.jsonl avait grossi au
    point que Python en mode texte ne pouvait plus l'ouvrir sous Windows
    (OSError: [Errno 22] Invalid argument), bloquant TOUTE écriture (donc
    toute ouverture de position, qui passe par un log avant). La rotation
    se base sur Path.stat() (jamais sur une ouverture du fichier), donc
    répare même un fichier déjà trop gros pour être ouvert."""
    path = tmp_path / "decisions.jsonl"
    path.write_text("x" * 200 + "\n", encoding="utf-8")

    logger = DecisionLogger(path=path, max_size_bytes=100)  # déjà au-dessus du seuil
    logger.log("candidate_scored", token_address="AFTER_ROTATION")

    rotated_files = list(tmp_path.glob("decisions.*.jsonl"))
    assert len(rotated_files) == 1
    assert "x" * 200 in rotated_files[0].read_text(encoding="utf-8")

    # Le nouveau fichier ne contient que l'écriture d'après rotation.
    events = logger.read_all()
    assert len(events) == 1
    assert events[0]["token_address"] == "AFTER_ROTATION"


def test_rotation_does_not_require_opening_the_oversized_file(tmp_path, monkeypatch):
    """Simule le cas réel : le fichier est trop gros pour être OUVERT (pas
    juste gros) -- la rotation doit quand même réussir, puisqu'elle ne
    passe jamais par open()."""
    path = tmp_path / "decisions.jsonl"
    path.write_text("x" * 200, encoding="utf-8")

    real_open = open

    def _open_that_refuses_the_oversized_file(file, *args, **kwargs):
        # Ne refuse QUE tant que le fichier physique est encore l'ancien
        # (trop gros) -- une fois la rotation faite (rename), le fichier au
        # même chemin est neuf/vide et doit pouvoir s'ouvrir normalement.
        if str(file) == str(path) and path.exists() and path.stat().st_size > 100:
            raise OSError(22, "Invalid argument")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _open_that_refuses_the_oversized_file)

    logger = DecisionLogger(path=path, max_size_bytes=100)
    logger.log("candidate_scored", token_address="RECOVERED")  # ne doit PAS lever

    monkeypatch.undo()
    events = logger.read_all()
    assert events[0]["token_address"] == "RECOVERED"
