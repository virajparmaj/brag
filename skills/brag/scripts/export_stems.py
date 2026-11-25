"""Export aligned, editable audio stems from a rendered Brag composition."""

import argparse
import json
import math
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory

SAMPLE_RATE = 48000
STEMS = ("dialogue", "music", "sfx")


class CompositionTags(HTMLParser):
    """Read clip fades and the root frame rate, which timeline JSON omits."""

    def __init__(self, path):
        super().__init__()
        self.clips = {}
        self.fps = 30
        self.feed(path.read_text(encoding="utf-8"))

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "audio" and values.get("id"):
            self.clips[values["id"]] = values
        if "data-composition-id" in values and "data-fps" in values:
            self.fps = int(values["data-fps"])


def hyperframes_command():
    binary = shutil.which("hyperframes")
    return [binary] if binary else ["npx", "hyperframes"]


def read_timeline(composition):
    result = subprocess.run(
        [*hyperframes_command(), "timeline", str(composition), "--json"],
        check=True, stdout=subprocess.PIPE, text=True,
    )
    timeline = json.loads(result.stdout)["timeline"]
    duration = float(timeline["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("The composition must have a positive duration.")
    clips = [
        row for track in timeline["tracks"] for row in track["rows"]
        if row["kind"] == "audio" and row.get("elementId")
        and row["absStart"] < duration and row["absEnd"] > row["absStart"]
    ]
    return duration, sorted(clips, key=lambda row: row["absStart"])


def asset_path(composition, source):
    path = (composition / source).resolve()
    if not path.is_relative_to(composition) or not path.is_file():
        raise ValueError(f"Expected a local composition asset: {source}")
    return path


def stem_for(clip):
    role = clip.get("role")
    if role in ("voiceover", "dialogue"):
        return "dialogue"
    if role in ("music", "sfx"):
        return role
    source = Path(clip["src"].lower())
    folders = source.parts[:-1]
    if "vo" in folders or source.stem.startswith("voiceover"):
        return "dialogue"
    if "sfx" in folders:
        return "sfx"
    if "music" in folders:
        return "music"
    if clip["id"].lower().startswith("vo"):
        return "dialogue"
    if clip["id"].lower().startswith("sfx"):
        return "sfx"
    return "music"


def gain_expression(points):
    """Build a piecewise linear gain on the clip-local clock."""
    times = [(float(point["t"]), float(point["v"])) for point in points]
    expression = str(times[-1][1])
    for (start, gain), (end, next_gain) in reversed(list(zip(times, times[1:]))):
        if end <= start:
            raise ValueError("Volume automation keyframes must be in time order.")
        ramp = f"{gain}+({next_gain - gain})*(t-{start})/{end - start}"
        expression = f"if(lt(t,{end}),{ramp},{expression})"
    return f"if(lt(t,{times[0][0]}),{times[0][1]},{expression})"


def atempo_filters(rate):
    filters = []
    while rate < 0.5:
        filters.append("atempo=0.5")
        rate *= 2
    while rate > 2:
        filters.append("atempo=2")
        rate /= 2
    if rate != 1:
        filters.append(f"atempo={rate}")
    return filters


def clip_filters(clip, attrs):
    start = clip["absStart"]
    duration = clip["duration"]
    source_start = clip.get("mediaStart") or 0
    rate = clip.get("playbackRate") or 1
    volume = clip.get("volume")
    if not 0.1 <= rate <= 10:
        raise ValueError(f"Unexpected playback rate for {clip['id']}.")
    filters = [
        f"aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo",
        f"atrim=start={source_start}:duration={duration * rate}",
        "asetpts=PTS-STARTPTS",
        *atempo_filters(rate),
        f"volume={1 if volume is None else float(volume)}",
    ]
    if clip.get("laneError"):
        raise ValueError(f"Invalid automation for {clip['id']}: {clip['laneError']}")
    for lane in clip.get("lanes", []):
        points = lane["points"]
        if lane["target"] == "rate" or (
            lane["target"] == "volume" and any(point.get("curve") for point in points)
        ):
            raise ValueError(f"Use linear volume and a constant rate for stem clip {clip['id']}.")
        if lane["target"] == "volume" and points:
            filters.append(f"volume='{gain_expression(points)}':eval=frame")
    fade_in = float(attrs.get("data-fade-in") or 0)
    fade_out = float(attrs.get("data-fade-out") or 0)
    if fade_in > 0:
        filters.append(f"afade=t=in:d={min(fade_in, duration)}")
    if fade_out > 0:
        fade_out = min(fade_out, duration)
        filters.append(f"afade=t=out:st={duration - fade_out}:d={fade_out}")
    if start < 0:
        filters.extend([f"atrim=start={-start}", "asetpts=PTS-STARTPTS"])
    else:
        filters.append(f"adelay={round(start * SAMPLE_RATE)}S:all=1")
    return ",".join(filters)


def write_stem(composition, clips, duration, output):
    args = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y"]
    encoding = ["-c:a", "pcm_s24le", "-ar", str(SAMPLE_RATE), "-ac", "2"]
    if not clips:
        args += ["-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo"]
    else:
        filters = []
        files = {}
        for index, clip in enumerate(clips):
            args += ["-i", str(asset_path(composition, clip["src"]))]
            file = asset_path(composition, clip["file"])
            if file not in files:
                files[file] = CompositionTags(file).clips
            attrs = files[file].get(clip["elementId"], {})
            filters.append(f"[{index}:a]{clip_filters(clip, attrs)}[a{index}]")
        inputs = "".join(f"[a{index}]" for index in range(len(clips)))
        filters.append(f"{inputs}amix=inputs={len(clips)}:normalize=0:dropout_transition=0,apad[mix]")
        args += ["-filter_complex", ";".join(filters), "-map", "[mix]"]
    subprocess.run([*args, "-t", str(duration), *encoding, str(output)], check=True)


def write_mix(video, duration, output):
    audio = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(video)],
        text=True,
    )
    if not audio.strip():
        write_stem(video.parent, [], duration, output)
        return
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(video),
         "-map", "0:a:0", "-af", "apad", "-t", str(duration), "-c:a", "pcm_s24le",
         "-ar", str(SAMPLE_RATE), "-ac", "2", str(output)],
        check=True,
    )


def export(output_dir):
    composition = (output_dir / "composition").resolve()
    video = output_dir / "brag.mp4"
    if not video.is_file():
        raise FileNotFoundError(f"Render the video before exporting: {video}")
    duration, clips = read_timeline(composition)
    groups = {stem: [] for stem in STEMS}
    for clip in clips:
        groups[stem_for(clip)].append(clip)

    with TemporaryDirectory(dir=output_dir) as scratch:
        staging = Path(scratch)
        notes = [
            f"Brag audio stems ({duration:g} seconds)",
            "48 kHz, 24-bit, stereo. Place each file at the start of the timeline.",
            "Clean stems carry clip-level gain and fades. The rendered mix includes the final processing.",
        ]
        for stem, group in groups.items():
            write_stem(composition, group, duration, staging / f"{stem}.wav")
            notes.append(f"\n{stem}.wav: {len(group)} clip(s)")
            notes.extend(f"  {clip['absStart']:7.3f}s  {clip['src']}" for clip in group)

        write_mix(video, duration, staging / "mix.wav")
        (staging / "README.txt").write_text("\n".join(notes) + "\n", encoding="utf-8")
        destination = output_dir / "stems"
        destination.mkdir(exist_ok=True)
        for file in staging.iterdir():
            file.replace(destination / file.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path, help="Folder containing composition/ and brag.mp4")
    args = parser.parse_args()
    try:
        export(args.output_dir.resolve())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Stem export failed: {error}\n")


if __name__ == "__main__":
    main()
