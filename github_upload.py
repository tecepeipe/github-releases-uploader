import os
import io
import math
import tempfile
import asyncio
import random
import aiohttp
import json
from tqdm import tqdm
from github import Auth, Github  # requires PyGithub
from github.GithubException import UnknownObjectException

# -----------------------------
# CONFIGURATION
# -----------------------------
GITHUB_TOKEN = "ghp_asdf" #YOUR_GITHUB_TOKEN
REPO_NAME = "tecepeipe/Tsundoku"
MAX_SIZE = 1850 * 1024 * 1024       # 1.85GB split size
CHUNK_SIZE = 4 * 1024 * 1024        # 4MB read chunks
MAX_PARALLEL = 4                    # parallel uploads
ROOT = r"D:\Filmez"                # Release tag

gh = Github(auth=Auth.Token(GITHUB_TOKEN))
repo = gh.get_repo(REPO_NAME)

# -----------------------------
# PROGRESS FILE WRAPPER 
# -----------------------------
class ProgressFile(io.BufferedReader):
    """
    A file-like object that updates tqdm progress on every read().
    aiohttp accepts this because it inherits from IOBase.
    """
    def __init__(self, raw, progress):
        super().__init__(raw)
        self.progress = progress

    def read(self, n=-1):
        chunk = super().read(n)
        if chunk:
            self.progress.update(len(chunk))
        return chunk


# -----------------------------
# RETRY FAILED UPLOADS
# -----------------------------
async def retry_async(func, *args, retries=5, base_delay=2, max_delay=30, exceptions=(Exception,), **kwargs):
    for attempt in range(1, retries + 1):
        try:
            return await func(*args, **kwargs)
        except exceptions as e:
            if attempt == retries:
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay *= 0.7 + random.random() * 0.6
            print(f"[Retry {attempt}/{retries}] Error: {e}. Retrying in {delay:.1f}s")
            await asyncio.sleep(delay)

# -----------------------------
# SPLIT LARGE FILES
# -----------------------------
def split_file(filepath, temp_dir):
    size = os.path.getsize(filepath)
    if size <= MAX_SIZE:
        return [filepath]

    filename = os.path.basename(filepath)
    num_parts = math.ceil(size / MAX_SIZE)
    part_paths = []

    with open(filepath, "rb") as f:
        for i in range(num_parts):
            part_path = os.path.join(temp_dir, f"{filename}.part{i+1}")
            with open(part_path, "wb") as p:
                remaining = MAX_SIZE
                while remaining > 0:
                    chunk = f.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    p.write(chunk)
                    remaining -= len(chunk)
            part_paths.append(part_path)

    return part_paths

# -----------------------------
# SHORTEN FILENAME FOR DISPLAY
# -----------------------------
def normalize_filename(name, start=35, end=20):
    if len(name) > start + end + 3:
        return name[:start] + "..." + name[-end:]
    if len(name) <= start + end + 3:
        return name.ljust(58)
    return name

# -----------------------------
# UPLOAD ASSET WITH PROGRESS
# -----------------------------
async def upload_asset_with_progress(release, file_path):
    filename = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    short = normalize_filename(filename)

    async with aiohttp.ClientSession() as session:
        upload_url = release.upload_url.replace("{?name,label}", f"?name={filename}")

        async def _send():
            with open(file_path, "rb") as raw, tqdm(
                total=file_size,
                unit="B",
                unit_scale=True,
                desc=f"Uploading {short}",
            ) as progress:

                wrapped = ProgressFile(raw, progress)

                headers = {
                    "Authorization": f"token {GITHUB_TOKEN}",
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(file_size),
                }

                async with session.post(upload_url, data=wrapped, headers=headers) as resp:
                    if resp.status in (200, 201):
                        return

                    text = await resp.text()

                    if resp.status == 422 and "already_exists" in text:
                        tqdm.write(f"Skipping existing asset: {filename}")
                        return

                    raise Exception(f"Upload failed ({resp.status}) for {filename}")

        await retry_async(_send)

# -----------------------------
# PROCESS A SINGLE FILE
# -----------------------------
async def process_single_file(full_path, folder_name, release, existing_assets, manifest):
    with tempfile.TemporaryDirectory() as temp_dir:
        parts = split_file(full_path, temp_dir)

        for part in parts:
            asset_name = os.path.basename(part)

            # Record the actual asset name in manifest
            manifest[folder_name].append(asset_name)

            if asset_name in existing_assets:
                continue

            await upload_asset_with_progress(release, part)


# -----------------------------
# PROCESS JOB
# -----------------------------
async def process_job(root_folder):

    job_name = os.path.basename(root_folder.rstrip("\\/"))
    tag = job_name.replace(" ", "_")

    # Create or fetch release
    try:
        release = repo.get_release(tag)
    except UnknownObjectException:
        release = repo.create_git_release(
            tag=tag,
            name=f"Job: {job_name}",
            message=f"Uploaded from job folder: {job_name}",
            draft=False,
            prerelease=False
        )

    existing_assets = {a.name for a in release.get_assets()}
    # Manifest structure: { "folder": [ "file1", "file2", ... ] }
    manifest = {}
    tasks = []

    for folder, _, files in os.walk(root_folder):
        if folder == root_folder:
            continue

        folder_name = os.path.basename(folder)
        manifest.setdefault(folder_name, [])

        for file in files:
            full_path = os.path.join(folder, file)

            tasks.append(
                asyncio.create_task(
                    process_single_file(full_path, folder_name, release, existing_assets, manifest)
                )
            )

            if len(tasks) >= MAX_PARALLEL:
                await asyncio.gather(*tasks)
                tasks = []

    if tasks:
        await asyncio.gather(*tasks)

    # -----------------------------
    # CREATE AND UPLOAD MANIFEST.JSON
    # -----------------------------
    manifest_path = os.path.join(tempfile.gettempdir(), "manifest.json")

    with open(manifest_path, "w", encoding="utf-8") as mf:
        json.dump(manifest, mf, indent=4)

    manifest_name = "manifest.json"

    if manifest_name not in existing_assets:
        print("Uploading manifest.json...")
        await upload_asset_with_progress(release, manifest_path)
    else:
        print("manifest.json already exists, skipping upload.")

# -----------------------------
# MAIN
# -----------------------------
if __name__ == "__main__":
    asyncio.run(process_job(ROOT))
