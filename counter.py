import os
import shutil
import stat

def handle_remove_readonly(func, path, exc):
    # Wordt aangeroepen als een bestand readonly is
    os.chmod(path, stat.S_IWRITE)
    func(path)

def remove_pycache(root_dir="."):
    """
    Verwijdert alle __pycache__ mappen en hun inhoud vanaf root_dir.
    """
    for dirpath, dirnames, filenames in os.walk(root_dir):
        if "__pycache__" in dirnames:
            pycache_path = os.path.join(dirpath, "__pycache__")
            print(f"Verwijderen: {pycache_path}")
            shutil.rmtree(pycache_path, onerror=handle_remove_readonly)


import os

def count_python_lines(directory):
    total_lines = 0
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith((".py", ".html", ".js", ".css")):
                file_path = os.path.join(root, file)
                with open(file_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    total_lines += len(lines)
    return total_lines

def start_count():
    backend_dir = "backend"
    frontend_dir = "frontend"
    backend_lines = count_python_lines(backend_dir)
    frontend_lines = count_python_lines(frontend_dir)
    print(f"Totaal aantal regels in '{backend_dir}': {backend_lines}")
    print(f"Totaal aantal regels in '{frontend_dir}': {frontend_lines}")



if __name__ == "__main__":
    remove_pycache()
    start_count()