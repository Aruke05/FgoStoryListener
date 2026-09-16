"""Opt-in real CLI + settings smoke test; no emulator connection or user data edits."""
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import time
import tkinter as tk
from tkinter import ttk

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT), str(PROJECT / ".tools" / "fgo-listener")]
import fgo_story_listener as app


def widgets(parent):
    for child in parent.winfo_children():
        yield child
        yield from widgets(child)


def main():
    names, status = app.refresh_codex_models(app.RuntimeConfig())
    assert names and status.startswith("已查询"), status
    print(status, names, flush=True)
    root = tk.Tk()
    root.title("FGO model catalog smoke test (no emulator)")
    root.config_data = app.RuntimeConfig(translation_model="keep-my-model")
    errors = []
    root.report_callback_exception = lambda *args: errors.append(str(args))
    try:
        app.Application.open_settings(root)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            root.update()
            boxes = [w for w in widgets(root) if isinstance(w, ttk.Combobox)]
            labels = [str(w.cget("text")) for w in widgets(root) if isinstance(w, ttk.Label)]
            if any(text.startswith("已查询 Codex model/list") for text in labels):
                break
            time.sleep(0.02)
        else:
            raise AssertionError("Settings refresh timed out")
        assert list(boxes[0].cget("values")) == names
        assert boxes[0].get() == "keep-my-model", "Refresh changed user selection"
        boxes[0].set(names[0])
        root.update()
        assert list(boxes[1].cget("values")) == app.codex_reasoning_efforts(names[0])
        dialog = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))
        refresh = next(w for w in widgets(dialog) if isinstance(w, ttk.Button) and w.cget("text") == "刷新模型列表")
        refresh.invoke()
        dialog.destroy()
        for _ in range(30):
            root.update()
            time.sleep(0.02)
        assert not errors, errors
        print("Settings: live catalog, selection preservation, efforts, close-during-refresh OK", flush=True)
    finally:
        root.destroy()

    # Isolate the smoke translation database/results from the user's captured story.
    with tempfile.TemporaryDirectory(prefix="fgo-model-smoke-") as temp:
        previous = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = temp
        store = app.HistoryStore(Path(temp) / "history.db")
        config = app.RuntimeConfig(
            translation_model=names[0], translation_reasoning="low", translation_fast_mode=False
        )
        worker = app.CodexTranslationWorker(store, config, queue.Queue())
        try:
            result = worker._invoke_exec(
                'Translate this Japanese line to Simplified Chinese. No tools. '
                'Return translations with id=1, speaker_zh="", zh=the translation, '
                'memory_updates=[], style_updates=[]. Japanese: こんにちは。'
            )
            assert result["translations"][0]["id"] == 1
            assert "你好" in result["translations"][0]["zh"], result
            print("Real codex exec:", json.dumps(result, ensure_ascii=False), flush=True)
        finally:
            worker._app_server.close()
            store.close()
            if previous is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = previous


if __name__ == "__main__":
    main()
