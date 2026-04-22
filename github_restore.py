import os
import json
import asyncio
import aiohttp
from tqdm import tqdm
from github import Auth, Github
from github.GithubException import UnknownObjectException

# -----------------------------
# CONFIG
# -----------------------------
GITHUB_TOKEN = "ghp_asdf"
REPO_NAME = "tecepeipe/Tsundoku"
TAG = "Fonts"  # Release job
OUTPUT_ROOT = r"F:\Fonts"

gh = Github(auth=Auth.Token(GITHUB_TOKEN))
repo = gh.get_repo(REPO_NAME)

# -----------------------------
# FUZZY LOGIC FOR FILE MATCHING
# -----------------------------
def normalize_for_match(name: str) -> str:
    name = name.lower()
    name = unicodedata.normalize("NFKD", name)
    name = re.sub(r"\.[a-z0-9]{1,5}$", "", name)  # remove extension
    name = re.sub(r"[^\w]+", " ", name)           # punctuation → space
    name = re.sub(r"\s+", " ", name).strip()
    return name

def fuzzy_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() * 100

def fuzzy_match(a: str, b: str, threshold=85.0) -> bool:
    return fuzzy_ratio(normalize_for_match(a), normalize_for_match(b)) >= threshold

def find_best_asset(name: str, assets):
    best = None
    best_score = 0
    target = normalize_for_match(name)

    for asset in assets:
        score = fuzzy_ratio(target, normalize_for_match(asset.name))
        if score > best_score:
            best_score = score
            best = asset

    return best if best_score >= 85 else None

# -----------------------------
# DOWNLOAD A SINGLE ASSET
# -----------------------------
async def download_asset(session, asset, dest_path):
    if os.path.exists(dest_path):
        return  # skip existing

    url = asset.url
    headers = {"Accept": "application/octet-stream",
               "Authorization": f"token {GITHUB_TOKEN}",
    }

    async with session.get(url, headers=headers) as resp:
        if resp.status != 200:
            raise Exception(f"Failed to download {asset.name}: {resp.status}")

        total = asset.size
        with open(dest_path, "wb") as f, tqdm(
            total=total,
            unit="B",
            unit_scale=True,
            desc=f"Downloading {asset.name}",
        ) as progress:

            async for chunk in resp.content.iter_chunked(4 * 1024 * 1024):
                f.write(chunk)
                progress.update(len(chunk))


# -----------------------------
# MERGE SPLIT PARTS
# -----------------------------
def merge_parts(folder_path, parts, output_name):
    output_path = os.path.join(folder_path, output_name)

    with open(output_path, "wb") as out:
        for part in parts:
            part_path = os.path.join(folder_path, part)
            with open(part_path, "rb") as p:
                while True:
                    chunk = p.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)

    # delete parts
    for part in parts:
        os.remove(os.path.join(folder_path, part))

    print(f"Restored: {output_name}")


# -----------------------------
# RESTORE JOB
# -----------------------------
async def restore_job(tag):

    # Fetch release
    try:
        release = repo.get_release(tag)
    except UnknownObjectException:
        print("Release not found")
        return

    # Download manifest.json
    manifest_asset = None
    for a in release.get_assets():
        if a.name == "manifest.json":
            manifest_asset = a
            break

    if not manifest_asset:
        print("manifest.json missing — cannot restore")
        return

    # Download manifest
    manifest_path = os.path.join(OUTPUT_ROOT, "manifest.json")
    async with aiohttp.ClientSession() as session:
        await download_asset(session, manifest_asset, manifest_path)

    # Load manifest
    with open(manifest_path, "r", encoding="utf-8") as mf:
        manifest = json.load(mf)

    # Build list of assets (no dict)
    asset_list = list(release.get_assets())

    # Download all assets
    async with aiohttp.ClientSession() as session:

        for folder, files in manifest.items():
            folder_path = os.path.join(OUTPUT_ROOT, folder)
            os.makedirs(folder_path, exist_ok=True)

            # Group parts by base filename
            file_groups = {}

            for name in files:
                if ".part" in name:
                    base = name.split(".part")[0]
                    file_groups.setdefault(base, []).append(name)
                else:
                    file_groups.setdefault(name, []).append(name)

            # Download all parts
            for base, parts in file_groups.items():

                # Download each part
                for part in parts:
                    asset = find_best_asset(part, asset_list)

                    if not asset:
                        print(f"Missing asset on GitHub: {part}")
                        continue

                    dest_path = os.path.join(folder_path, part)
                    await download_asset(session, asset, dest_path)

                # Merge if split
                if len(parts) > 1:
                    parts_sorted = sorted(parts, key=lambda x: int(x.split(".part")[1]))
                    merge_parts(folder_path, parts_sorted, base)

    print("Restore complete.")


# -----------------------------
# MAIN
# -----------------------------
if __name__ == "__main__":
    asyncio.run(restore_job(TAG))
