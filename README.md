# AutoValor Pro Colombia

AutoValor Pro Colombia es una aplicación web que calcula el valor aproximado de un carro usado en Colombia.
El usuario elige marca, línea, año, motor, caja y versión, escribe el kilometraje y la aplicación le muestra
un valor estimado con un rango de precio. También tiene un simulador, un historial de consultas y un panel
de administración para actualizar vehículos y precios.

## Organización de carpetas

| Carpeta / archivo | Qué contiene |
|---|---|
| `app.py` | Servidor web (rutas de las páginas y de la API) |
| `valuation.py`, `catalog_selectors.py`, `catalog_utils.py`, `transmissions.py` | Lógica de la valoración y de los selectores |
| `db.py`, `migrations.py` | Conexión, creación y actualización de la base de datos |
| `engine_audit.py`, `transmission_audit.py` | Revisión de motores y cajas de cada versión |
| `importers/` | Carga de la guía de precios a la base |
| `templates/` | Páginas HTML (Jinja2) |
| `static/` | Recursos estáticos: CSS, JavaScript e imágenes |
| `database/schema.sql` | Script SQL con la estructura de la base (tablas, relaciones y restricciones) |
| `database/autovalor.db.zip` | Base de datos SQLite con todos los vehículos y precios (comprimida; se descomprime sola al iniciar) |
| `data/` | Datos iniciales en CSV (vehículos, precios y fichas técnicas) |
| `scripts/` | Scripts para crear la base, importar datos y hacer auditorías |
| `tests/` | Pruebas automáticas |
| `docs/` | Documentación técnica, manual de usuario y presentación |

## Tecnologías

- **Lenguaje:** Python 3.10 o superior, JavaScript, HTML y CSS
- **Framework web:** FastAPI con el servidor Uvicorn
- **Plantillas:** Jinja2
- **Base de datos:** SQLite
- **Dependencias:** están en `requirements.txt` (fastapi, uvicorn, Jinja2, python-dotenv, python-multipart, httpx, itsdangerous)

## Instalación y ejecución local

1. Instala Python 3.10 o superior desde https://www.python.org (en Windows marca la casilla *Add Python to PATH*).
2. Descarga el proyecto (botón verde **Code → Download ZIP**) y descomprímelo.
3. Abre la carpeta del proyecto y ejecuta:
   - **Windows:** doble clic en `iniciar_windows.bat`
   - **Mac o Linux:** en una terminal, `bash iniciar_mac_linux.sh`
4. El script crea el entorno, instala las dependencias, prepara la base de datos y abre la página en
   http://127.0.0.1:5000
5. La primera vez se crea el archivo `.env` con la contraseña del panel de administración (se muestra en la
   ventana). Ese archivo no se sube al repositorio.

Instalación manual (opcional):

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
python scripts/setup_env.py
python scripts/init_db.py
python app.py
```

Para correr las pruebas: `python -m unittest discover -s tests`

## Base de datos

- **Estructura:** `database/schema.sql` crea las tablas; `migrations.py` agrega los cambios posteriores.
- **Datos iniciales:** `scripts/init_db.py` crea la base y carga los datos de `data/` si está vacía.
  La base `database/autovalor.db.zip` ya viene lista con el catálogo completo para la demostración; al iniciar la aplicación se descomprime sola como `database/autovalor.db`.

## Publicación en internet

El archivo `render.yaml` permite publicar la aplicación en Render (render.com) conectando este repositorio.

## Créditos

Desarrollado por:

- **Tomás Hincapié**
- **Sebastián Franco**
