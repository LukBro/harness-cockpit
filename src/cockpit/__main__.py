import uvicorn

from cockpit.settings import settings


def main() -> None:
    uvicorn.run("cockpit.api.app:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
