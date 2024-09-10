"""This provides a check, if all filenames are correct and if all IDs used by GOG and Steam fixes are valid."""

import sys
from pathlib import Path
from urllib.request import urlopen, Request
from http.client import HTTPSConnection
from typing import Any
from collections.abc import Iterator, Generator

import ijson

# Represents a valid API endpoint, where the first element is the host, second
# is the url (e.g., store.steampowered.com and store.steampowered.com). The API
# endpoint will be used to validate local gamefix modules IDs against. Assumes
# that the API is associated to the gamefix directory when passed to a function
ApiEndpoint = tuple[str, str]


headers = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0',
    'Accept': 'application/font-woff2;q=1.0,application/font-woff;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}

# Steam games that are no longer on sale, but are valid IDs
whitelist_steam = {231990, 4730, 105400, 321040, 12840, 7850}


def check_steamfixes(project: Path, url: str, api: ApiEndpoint) -> None:
    """Verifies if the name of Steam gamefix modules are valid entries.

    Raises a ValueError if the ID is not found upstream
    """
    appids = set()

    # Get all IDs
    for file in project.joinpath('gamefixes-steam').glob('*'):
        appid = file.name.removesuffix('.py')
        if not appid.isnumeric():
            continue
        appids.add(int(appid))

    # Check the IDs against ours
    print(f"Validating Steam app ids against '{url}'...", file=sys.stderr)
    with urlopen(Request(url, headers=headers), timeout=500) as r:
        for obj in ijson.items(r, 'applist.apps.item'):
            if obj['appid'] in appids:
                print(f'Removing Steam app id: "{obj["appid"]}"', file=sys.stderr)
                appids.remove(obj['appid'])
            if not appids:
                break

    # Double check that the ID is valid. It's possible that it is but
    # wasn't returned from the api in `url` for some reason
    if appids:
        host, endpoint = api
        conn = HTTPSConnection(host)

        print(f"Validating Steam app ids against '{host}'...", file=sys.stderr)
        for appid in appids.copy():
            conn.request('GET', f'{endpoint}{appid}')
            r = conn.getresponse()
            parser: Iterator[tuple[str, str, Any]] = ijson.parse(r)

            for prefix, _, value in parser:
                if prefix == f'{appid}.success' and isinstance(value, bool) and value:
                    print(f'Removing Steam app id: "{appid}"', file=sys.stderr)
                    appids.remove(appid)
                    break

            if not appids:
                break

            r.read()

        conn.close()

    print(f'Remaining Steam app ids: {appids}', file=sys.stderr)
    for appid in appids:
        if appid not in whitelist_steam:
            err = f'Steam app id is invalid: {appid}'
            raise ValueError(err)


def check_gogfixes(project: Path, url: str, api: ApiEndpoint) -> None:
    """Verifies if the name of GOG gamefix modules are valid entries.

    Raises a ValueError if the ID is not found upstream, in gamefixes-steam
    or in the umu-database
    """
    appids = set()

    # Find all IDs in batches of 50. The gog api enforces 50 ids per request
    # See https://gogapidocs.readthedocs.io/en/latest/galaxy.html#get--products
    for gogids in _batch_generator(project.joinpath('gamefixes-gog')):
        sep = '%2C'  # Required comma separator character. See the docs.
        appids = gogids.copy()

        print(f'Validating GOG app ids against "{url}"...', file=sys.stderr)
        with urlopen(
            Request(f'{url}{sep.join(appids)}', headers=headers), timeout=500
        ) as r:
            for obj in ijson.items(r, 'item'):
                # Like Steam's, app ids are integers
                if (appid := str(obj['id'])) in appids:
                    print(f'Removing GOG app id: "{appid}"', file=sys.stderr)
                    appids.remove(appid)
                if not appids:
                    break

    # IDs may be links to Steam fixes.
    if appids:
        print('Validating GOG app ids against Steam app ids...', file=sys.stderr)
        for file in project.joinpath('gamefixes-steam').glob('*'):
            if (appid := file.name.removesuffix('.py')) in appids:
                print(f'Removing GOG app id: "{appid}"', file=sys.stderr)
                appids.remove(appid)
            if not appids:
                break

    # IDs may not be using upstream's ID (e.g., Alien Breed). Check all ids against the umu database
    if appids:
        host, endpoint = api
        conn = HTTPSConnection(host)
        conn.request('GET', endpoint)
        r = conn.getresponse()

        print(f'Validating GOG app ids against "{host}"...', file=sys.stderr)
        for obj in ijson.items(r, 'item'):
            if (appid := str(obj['umu_id']).removeprefix('umu-')) in appids:
                print(f'Removing GOG app id: "{appid}"', file=sys.stderr)
                appids.remove(appid)
            if not appids:
                break

        conn.close()

    print(f'Remaining GOG app ids: {appids}', file=sys.stderr)
    if appids:
        err = (
            'The following GOG app ids are invalid or are missing entries'
            f' in the umu database: {appids}'
        )
        raise ValueError(err)


def _batch_generator(gamefix: Path, size: int = 50) -> Generator[set[str], Any, Any]:
    appids = set()
    # Keep track of the count because some APIs enforce limits
    count = 0

    # Process only umu-* app ids
    for file in gamefix.glob('*'):
        if not file.name.startswith('umu-'):
            continue
        appid = file.name.removeprefix('umu-').removesuffix('.py')
        appids.add(appid)
        if count == size:
            yield appids
            appids.clear()
            count = 0
            continue
        count += 1

    yield appids


def check_links(root: Path) -> None:
    """Check for broken symbolic links"""
    gamefixes = [
        file
        for file in root.glob('gamefixes-*/*.py')
        if not file.name.startswith(('__init__.py', 'default.py', 'winetricks-gui.py'))
    ]

    print('Checking for broken symbolic links...', file=sys.stderr)
    for module in gamefixes:
        print(f'{module.parent.name}/{module.name}', file=sys.stderr)
        if module.is_symlink() and not module.exists():
            err = f'The following file is not a valid symbolic link: {module}'
            raise FileNotFoundError(err)


def check_filenames(root: Path) -> None:
    """Check for expected file names.

    All files in non-steam gamefixes are expected to start with 'umu-'
    All files in steam gamefixes are expected to have a numeric name
    """
    gamefixes = [
        file
        for file in root.glob('gamefixes-*/*.py')
        if not file.name.startswith(('__init__.py', 'default.py', 'winetricks-gui.py'))
    ]

    print('Checking for expected file names...', file=sys.stderr)
    for module in gamefixes:
        print(f'{module.parent.name}/{module.name}', file=sys.stderr)
        is_steam = module.parent.name.startswith('gamefixes-steam')
        if not module.exists():
            err = f'The following file does not exist: {module.parent.name}/{module}'
            raise FileNotFoundError(err)
        elif is_steam and not module.stem.isnumeric():
            err = f'The following Steam fix filename is invalid: {module}'
            raise ValueError(err)
        elif not is_steam and not module.name.startswith('umu-'):
            err = f'The following file does not start with "umu-": {module}'
            raise ValueError(err)


def main() -> None:
    """Validate gamefixes modules."""
    # Top-level project directory that is expected to contain gamefix directories
    project = Path(__file__).parent.parent.parent
    print(project)

    # Steam API to acquire a single id. Used as fallback in case some IDs could
    # not be validated. Unforutnately, this endpoint does not accept a comma
    # separated list of IDs so we have to make one request per ID after making
    # making a request to `api.steampowered.com`.
    # NOTE: There's neither official nor unofficial documentation. Only forum posts
    # See https://stackoverflow.com/questions/46330864/steam-api-all-games
    steamapi: ApiEndpoint = ('store.steampowered.com', '/api/appdetails?appids=')

    # UMU Database, that will be used to validate umu gamefixes ids against
    # See https://github.com/Open-Wine-Components/umu-database/blob/main/README.md
    umudb_gog: ApiEndpoint = ('umu.openwinecomponents.org', '/umu_api.php?store=gog')

    # Steam API
    # Main API used to validate steam gamefixes
    # NOTE: There's neither official nor unofficial documentation. Only forum posts
    # See https://stackoverflow.com/questions/46330864/steam-api-all-games
    steampowered = (
        'https://api.steampowered.com/ISteamApps/GetAppList/v0002/?format=json'
    )

    # GOG API
    # See https://gogapidocs.readthedocs.io/en/latest/galaxy.html#get--products
    gogapi = 'https://api.gog.com/products?ids='

    check_links(project)
    check_filenames(project)
    check_steamfixes(project, steampowered, steamapi)
    check_gogfixes(project, gogapi, umudb_gog)


if __name__ == '__main__':
    main()