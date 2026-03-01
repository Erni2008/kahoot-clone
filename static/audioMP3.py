import argparse
import os
import shutil
import subprocess
from pathlib import Path

def run(cmd: list[str]) -> None:
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)

def ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise SystemExit(
            "ffmpeg не найден. Установи: brew install ffmpeg\n"
            "Или проверь PATH."
        )

def build_ffmpeg_cmd(
    input_path: Path,
    output_path: Path,
    bitrate: str,
    start: str | None,
    end: str | None,
    normalize: bool,
) -> list[str]:
    cmd = ["ffmpeg", "-y"]

    # Быстрый seek (если указан start)
    if start:
        cmd += ["-ss", start]

    cmd += ["-i", str(input_path)]

    # Обрезка до end (если указан)
    if end:
        cmd += ["-to", end]

    # Только аудио
    cmd += ["-vn", "-c:a", "libmp3lame", "-b:a", bitrate]

    # Нормализация громкости (опционально)
    if normalize:
        cmd += ["-af", "loudnorm"]

    cmd += [str(output_path)]
    return cmd

def convert_one(
    input_path: Path,
    out_dir: Path,
    bitrate: str,
    start: str | None,
    end: str | None,
    normalize: bool,
) -> Path:
    if not input_path.exists():
        raise SystemExit(f"Файл не найден: {input_path}")

    out_dir.mkdir(parents=True, exist_ok=True)

    out_name = input_path.stem + ".mp3"
    output_path = out_dir / out_name

    cmd = build_ffmpeg_cmd(input_path, output_path, bitrate, start, end, normalize)
    run(cmd)
    return output_path

def iter_mp4_files(path: Path):
    if path.is_file():
        yield path
    else:
        for p in sorted(path.rglob("*.mp4")):
            yield p

def main():
    parser = argparse.ArgumentParser(
        description="Конвертация MP4 → MP3 через ffmpeg (один файл или папка)."
    )
    parser.add_argument("input", help="Путь к mp4 файлу или папке с mp4")
    parser.add_argument(
        "-o", "--out",
        default="game/static/audio",
        help="Папка вывода (по умолчанию: game/static/audio)"
    )
    parser.add_argument(
        "--bitrate",
        default="192k",
        help="Битрейт MP3 (например 128k, 192k, 256k). По умолчанию 192k"
    )
    parser.add_argument(
        "--start",
        default=None,
        help="Начало фрагмента (например 00:00:12)"
    )
    parser.add_argument(
        "--end",
        default=None,
        help="Конец фрагмента (например 00:00:20)"
    )
    parser.add_argument(
        "--normalize",
        action="store_true",
        help="Нормализовать громкость (loudnorm)"
    )

    args = parser.parse_args()

    ensure_ffmpeg()

    input_path = Path(args.input).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()

    files = list(iter_mp4_files(input_path))
    if not files:
        raise SystemExit("Не найдено ни одного .mp4")

    print(f"Найдено MP4: {len(files)}")
    for f in files:
        try:
            out = convert_one(
                f, out_dir, args.bitrate, args.start, args.end, args.normalize
            )
            print(f"✅ Готово: {out}")
        except subprocess.CalledProcessError as e:
            print(f"❌ Ошибка конвертации {f.name}: {e}")

if __name__ == "__main__":
    main()