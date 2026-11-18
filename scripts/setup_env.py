from pathlib import Path
import secrets

root = Path(__file__).resolve().parents[1]
env_file = root / ".env"
if env_file.exists():
    print("Ya existe .env; no se modificó para proteger tu configuración.")
    print("Si necesitas cambiar la clave, edita .env y reinicia la aplicación.")
else:
    secret_key = secrets.token_urlsafe(48)
    admin_password = secrets.token_urlsafe(18)
    env_file.write_text(
        "# Claves locales generadas en la primera ejecución. No compartas este archivo.\n"
        f"SECRET_KEY={secret_key}\n"
        f"ADMIN_PASSWORD={admin_password}\n"
        "# AUTOVALOR_DB=database/autovalor.db\n",
        encoding="utf-8",
    )
    print("Se creó .env con claves aleatorias para esta instalación local.")
    print("Contraseña inicial del panel administrativo:")
    print(admin_password)
    print("También queda guardada en .env. Cambia la contraseña si lo necesitas y no compartas el archivo.")
