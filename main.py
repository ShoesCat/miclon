import msvcrt
import subprocess
import sys
from pathlib import Path


def main():
    v2_path = Path(__file__).with_name("v2.py")
    v3_path = Path(__file__).with_name("v3.py")
    v4_path = Path(__file__).with_name("v4.py")

    while True:
        print("\n=== MICLON ===")
        print("1. Démarrer V2")
        print("2. Démarrer V3 (commande micro depuis le téléphone)")
        print("3. Démarrer V4 (HTTPS avec certificat automatique)")
        print("Échap. Quitter")
        print("Votre choix : ", end="", flush=True)

        choice = msvcrt.getwch()
        print()

        if choice == "\x1b":
            print("Au revoir.")
            return
        if choice == "1":
            result = subprocess.run([sys.executable, str(v2_path)], check=False)
            if result.returncode == 0:
                print("Au revoir.")
                return
        if choice == "2":
            result = subprocess.run([sys.executable, str(v3_path)], check=False)
            if result.returncode == 0:
                print("Au revoir.")
                return
        if choice == "3":
            result = subprocess.run([sys.executable, str(v4_path)], check=False)
            if result.returncode == 0:
                print("Au revoir.")
                return


if __name__ == "__main__":
    main()
