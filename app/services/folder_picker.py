from pathlib import Path


class MacFolderPicker:
    def choose(self) -> Path | None:
        import subprocess

        result = subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                'POSIX path of (choose folder with prompt "选择图片文件夹")',
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        return Path(value) if value else None
