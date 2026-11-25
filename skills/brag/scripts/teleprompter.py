"""Export caption sidecars and a seekable teleprompter for a narrated Brag."""

import argparse
import json
import re
import shutil
import subprocess
from html import escape
from pathlib import Path

from export_stems import CompositionTags, asset_path, hyperframes_command, read_timeline, stem_for


def timestamp(seconds, separator):
    milliseconds = round(seconds * 1000)
    whole_seconds, remainder = divmod(milliseconds, 1000)
    hours, minutes = divmod(whole_seconds // 60, 60)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds % 60:02d}{separator}{remainder:03d}"


def timecode(seconds, fps):
    seconds, frame = divmod(round(seconds * fps), fps)
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds % 60:02d}:{frame:02d}"


def speech_bounds(composition, clip, duration):
    offset = clip.get("mediaStart") or 0
    rate = clip.get("playbackRate") or 1
    length = min(clip["duration"], duration - clip["absStart"])
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-ss", str(offset),
         "-i", str(asset_path(composition, clip["src"])), "-t", str(length * rate),
         "-af", "silencedetect=noise=-35dB:d=0.12", "-f", "null", "-"],
        check=True, stderr=subprocess.PIPE, text=True,
    )
    # Ignore internal pauses when setting caption boundaries. They remain in the guide track.
    leading = re.search(r"silence_start: -?0(?:\.0+)?\s.*?silence_end: ([\d.]+)", result.stderr, re.DOTALL)
    trailing = list(re.finditer(r"silence_start: ([\d.]+).*?silence_end: ([\d.]+)", result.stderr, re.DOTALL))
    start = float(leading[1]) / rate if leading else 0
    end = length
    if trailing and float(trailing[-1][2]) >= length * rate - 0.02:
        end = float(trailing[-1][1]) / rate
    if end <= start:
        raise ValueError(f"No audible dialogue in {clip['elementId']}.")
    return clip["absStart"] + start, clip["absStart"] + end


def cue_words(text, start, end):
    words = text.split()
    weights = [len(word.strip(".,!?")) + 2 for word in words]
    total_weight = sum(weights)
    elapsed = 0
    entries = []
    for word, weight in zip(words, weights):
        onset = start + (end - start) * elapsed / total_weight
        entries.append({"word": word, "start": onset})
        elapsed += weight
    return entries


def read_cues(composition, duration, clips):
    metadata = json.loads((composition / "assets/vo/script.json").read_text(encoding="utf-8"))
    lines = metadata["lines"]
    cues = []
    for clip in clips:
        if stem_for(clip) != "dialogue":
            continue
        clip_id = clip["elementId"]
        text = lines.get(clip_id, "").strip()
        if not text:
            raise ValueError(f"Missing narration text for audio clip {clip_id}.")
        start, end = speech_bounds(composition, clip, duration)
        cues.append({"id": clip_id, "text": text, "start": start, "end": end,
                     "words": cue_words(text, start, end)})
    if not cues:
        raise ValueError("The composition has no timed dialogue.")
    return metadata.get("voice", ""), cues


def write_captions(stems, cues, duration, fps, voice):
    for extension, separator in (("srt", ","), ("vtt", ".")):
        header = "WEBVTT\n\n" if extension == "vtt" else ""
        entries = []
        for number, cue in enumerate(cues, start=1):
            start = timestamp(cue["start"], separator)
            end = timestamp(cue["end"], separator)
            text = escape(cue["text"], quote=False)
            entries.append(f"{number}\n{start} --> {end}\n{text}\n\n")
        (stems / f"transcript.{extension}").write_text(header + "".join(entries), encoding="utf-8")
    entries = [f"{timecode(cue['start'], fps)} – {timecode(cue['end'], fps)}  {cue['text']}" for cue in cues]
    (stems / "transcript.txt").write_text("\n".join(entries) + "\n", encoding="utf-8")
    data = {"voice": voice, "duration": duration, "fps": fps, "lines": cues}
    (stems / "transcript.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_composition(project, guide, cues, duration, fps):
    assets = project / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(guide, assets / "guide.wav")
    timing = json.dumps([[cue["start"], [word["start"] for word in cue["words"]]] for cue in cues])
    lines = "\n".join(
        "<p>" + " ".join(f"<span>{escape(word['word'])}</span>" for word in cue["words"]) + "</p>"
        for cue in cues
    )
    template = Path(__file__).with_name("teleprompter.html").read_text(encoding="utf-8")
    parts = {"DURATION": str(duration), "FPS": str(fps), "LINES": lines, "TIMING": timing}
    page = re.sub(r"\{\{(DURATION|FPS|LINES|TIMING)\}\}", lambda match: parts[match[1]], template)
    (project / "index.html").write_text(page, encoding="utf-8")


def build(output_dir, render):
    composition = (output_dir / "composition").resolve()
    stems = output_dir / "stems"
    guide = stems / "dialogue.wav"
    if not guide.is_file():
        raise FileNotFoundError("Export stems before generating the teleprompter.")
    duration, clips = read_timeline(composition)
    fps = CompositionTags(composition / "index.html").fps
    voice, cues = read_cues(composition, duration, clips)
    write_captions(stems, cues, duration, fps, voice)
    project = output_dir / "teleprompter"
    write_composition(project, guide, cues, duration, fps)
    if render:
        cli = hyperframes_command()
        subprocess.run([*cli, "check", str(project)], check=True)
        subprocess.run(
            [*cli, "render", str(project), "--quality", "delivery", "--output", str(stems / "teleprompter.mp4")],
            check=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--render", action="store_true", help="Render the MP4 narration guide")
    args = parser.parse_args()
    try:
        build(args.output_dir.resolve(), args.render)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Teleprompter export failed: {error}\n")


if __name__ == "__main__":
    main()
