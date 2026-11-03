import uvicorn

from cockpit.settings import settings
from runner.maskowanie import zainstaluj_maskowanie


def main() -> None:
    zainstaluj_maskowanie()
    uvicorn.run("cockpit.api.app:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
