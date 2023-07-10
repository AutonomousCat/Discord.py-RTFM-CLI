""" Based on: https://github.com/Rapptz/RoboDanny/blob/rewrite/cogs/api.py """ 
import re
import zlib
import io
import json
import os
import sys
import httpx
from rich.console import Console
from rich.panel import Panel
from rich.tree import Tree
from rich.console import Console
from typing import Callable, Generator, Optional, Iterable, TypeVar

DATA_DIRECTORY = 'rtfm_cache'
DEFAULT_PAGE_TYPE = 'stable'
RTFM_PAGE_TYPES = {
    'stable': 'https://discordpy.readthedocs.io/en/stable',
    'latest': 'https://discordpy.readthedocs.io/en/latest',
    'python': 'https://docs.python.org/3',
}
CACHE_EXTENSION = '_cache.json'
DATA_FULL_PATH = os.path.join(os.getcwd(), DATA_DIRECTORY)
CACHE_FILES = tuple(key+CACHE_EXTENSION for key in RTFM_PAGE_TYPES)

# https://rich.readthedocs.io/en/stable/appendix/colors.html
STYLE_GENERAL = 'bold green'
STYLE_NOTICE = 'bold yellow'
STYLE_WARNING = 'bold red'

STYLE_LINK = 'bold purple4'
STYLE_LINK_ID = 'grey42'
STYLE_BORDER = 'slate_blue3'

T = TypeVar('T')

def finder(text: str, collection: Iterable[T], *, key: Optional[Callable[[T], str]] = None) -> list:
    # https://github.com/Rapptz/RoboDanny/blob/rewrite/cogs/utils/fuzzy.py#L325-L350
    suggestions: list[tuple[int, T]] = []
    text = str(text)
    pat = '.*?'.join(map(re.escape, text))
    regex = re.compile(pat, flags=re.IGNORECASE)
    for item in collection:
        to_search = key(item) if key else str(item)
        r = regex.search(to_search)
        if r:
            suggestions.append((r.start(), item))
    def sort_key(tup: tuple[int, T]) -> tuple[int, str | T]:
        if key:
            return tup[0], key(tup[1])
        return tup
    return [z for _, z in sorted(suggestions, key=sort_key)]

class SphinxObjectFileReader:
    # https://github.com/Rapptz/RoboDanny/blob/rewrite/cogs/api.py#L119-L149
    BUFSIZE = 16 * 1024
    def __init__(self, buffer: bytes):
        self.stream = io.BytesIO(buffer)
    def readline(self) -> str:
        return self.stream.readline().decode('utf-8')
    def skipline(self) -> None:
        self.stream.readline()
    def read_compressed_chunks(self) -> Generator[bytes, None, None]:
        decompressor = zlib.decompressobj()
        while True:
            chunk = self.stream.read(self.BUFSIZE)
            if len(chunk) == 0:
                break
            yield decompressor.decompress(chunk)
        yield decompressor.flush()
    def read_compressed_lines(self) -> Generator[str, None, None]:
        buf = b''
        for chunk in self.read_compressed_chunks():
            buf += chunk
            pos = buf.find(b'\n')
            while pos != -1:
                yield buf[:pos].decode('utf-8')
                buf = buf[pos + 1 :]
                pos = buf.find(b'\n')

class RTFM:
    console = Console()
    def __init__(self):
        self.current_page_type = DEFAULT_PAGE_TYPE
        self.render_tree = False
        self.refresh_cache = False
        self.rtfm_cache = {}
        self.console_output: dict[str, Optional[Panel]] = {
            'docs_links': None,
            'docs_tree': None
        }
        self.run()
        
    def run(self, refresh_cache=False):
        if not os.path.isdir(DATA_FULL_PATH):
            self.console.print(
                f"Missing a '{DATA_DIRECTORY}' folder, creating one in: '{DATA_FULL_PATH}'",
                style=STYLE_GENERAL
            )
            os.mkdir(DATA_FULL_PATH)

        if refresh_cache:
            for file in CACHE_FILES:
                cache_file_path = os.path.join(DATA_FULL_PATH, file)
                if os.path.isfile(cache_file_path):
                    os.remove(cache_file_path)
            missing_files = CACHE_FILES
        else:
            self.console.print(
                f"""Looking for cache files: '{"', '".join(CACHE_FILES)}'""",
                style=STYLE_GENERAL
            )
            missing_files = [file for file in RTFM_PAGE_TYPES if not os.path.isfile(
                            os.path.join(DATA_FULL_PATH, file+CACHE_EXTENSION))]

        if missing_files:
            self.console.print(
                f"Downloading {len(missing_files)} missing cache files... "
                "Note: You can re-download the cache any time by entering 'refresh'.",
                style=STYLE_NOTICE
            )
            self.build_rtfm_lookup_table(missing_files=missing_files)

        available_cache_files = []

        for key in RTFM_PAGE_TYPES:
            cache_file_path = os.path.join(DATA_FULL_PATH, key + CACHE_EXTENSION)
            if os.path.isfile(cache_file_path):
                with open(cache_file_path, 'r') as fp:
                    self.rtfm_cache[key] = json.load(fp)
                available_cache_files.append(key)

        if len(self.rtfm_cache) == len(RTFM_PAGE_TYPES):
            self.console.print(
                f"Ready for use. Current page type: '{self.current_page_type}'",
                style=STYLE_GENERAL
            )
        elif 1 < len(self.rtfm_cache) < len(RTFM_PAGE_TYPES):
            self.console.print(
                f"""Ready for use, but the only cache types available are: '{"', '".join(self.rtfm_cache)}'""",
                style=STYLE_WARNING
            )

            if self.current_page_type not in available_cache_files:
                self.current_page_type = available_cache_files[0]
                self.console.print(f"Current page type set to: '{self.current_page_type}'", style=STYLE_NOTICE)
        else:
            self.console.print('Cache could not be loaded. Exiting...')
            sys.exit()

    def parse_object_inv(self, stream: SphinxObjectFileReader) -> dict[str, str]:
        # https://github.com/Rapptz/RoboDanny/blob/rewrite/cogs/api.py#L191-L244
        result: dict[str, str] = {}
        inv_version = stream.readline().rstrip()
        if inv_version != '# Sphinx inventory version 2':
            raise RuntimeError('Invalid objects.inv file version.')
        projname = stream.readline().rstrip()[11:]
        version = stream.readline().rstrip()[11:]
        line = stream.readline()
        if 'zlib' not in line:
            raise RuntimeError('Invalid objects.inv file, not z-lib compatible.')
        entry_regex = re.compile(r'(?x)(.+?)\s+(\S*:\S*)\s+(-?\d+)\s+(\S+)\s+(.*)')
        for line in stream.read_compressed_lines():
            match = entry_regex.match(line.rstrip())
            if not match:
                continue
            name, directive, prio, location, dispname = match.groups()
            domain, _, subdirective = directive.partition(':')
            if directive == 'py:module' and name in result or \
                subdirective == 'opcode':
                continue
            if directive == 'std:doc' or \
                subdirective == 'label':
                continue
            if location.endswith('$'):
                location = location[:-1] + name
            key = name if dispname == '-' else dispname
            prefix = f'{subdirective}:' if domain == 'std' else ''
            if projname == 'discord.py':
                key = key.replace('discord.ext.commands.', '').replace('discord.', '')
            result[f'{prefix}{key}'] = location
        return result

    def save_cache(self, name: str, cache: dict):
        name = name + CACHE_EXTENSION
        with open(os.path.join(DATA_FULL_PATH, name), 'w') as fp:
            json.dump(cache, fp)

    def build_rtfm_lookup_table(self, missing_files: Optional[Iterable[str]] = None):
        try:
            if missing_files is None:
                missing_files = RTFM_PAGE_TYPES.keys()

            for key in missing_files:
                cache_file_path = os.path.join(DATA_FULL_PATH, key)
                if os.path.isfile(cache_file_path):
                    with open(cache_file_path, 'r') as fp:
                        self.rtfm_cache[key] = json.load(fp)
                else:
                    with httpx.Client() as session:
                        response = session.get(RTFM_PAGE_TYPES[key] + '/objects.inv')
                        self.console.print(
                            f"Got status code '{response.status_code}' when downloading: '{response.url}'",
                            style=STYLE_GENERAL if response.status_code == 200 else STYLE_WARNING)
                        if response.status_code == 200:
                            stream = SphinxObjectFileReader(response.content)
                            self.rtfm_cache[key] = self.parse_object_inv(stream)
                            if key in self.rtfm_cache: 
                                self.save_cache(key, self.rtfm_cache[key])
                            else:
                                self.console.print(f"Failed to parse 'objects.inv' for key '{key}'", style=STYLE_WARNING)
        except Exception as e:
            self.console.print((
                f'An error occurred during cache processing: {type(e).__name__}: {str(e)}\n'
                'Try restarting this script.'),
                style=STYLE_WARNING)

    def do_rtfm(self, input):
        object = re.sub(r'^(?:discord\.(?:ext\.)?)?(?:commands\.)?(.+)', r'\1', input)
        cache = list(self.rtfm_cache[self.current_page_type].items())
        matches = finder(object, cache, key=lambda t: t[0])[:8]
        self.console_output['docs_tree'] = None
        self.console_output['docs_links'] = None
        if matches:
            if self.render_tree:
                nodes = {}
                for key, value in matches:
                    parts = key.split('.')
                    for i in range(len(parts)):
                        current_hierarchy = '.'.join(parts[:i+1])
                        if current_hierarchy not in nodes:
                            parent_hierarchy = '.'.join(parts[:i])
                            parent_node = nodes[parent_hierarchy] if parent_hierarchy in nodes else self.tree
                            nodes[current_hierarchy] = parent_node.add(parts[i])
                self.console_output['docs_tree'] = Panel.fit(self.tree,
                                                    border_style=STYLE_BORDER,
                                                    subtitle=f"matches for '{input}'")
            else:
                match_urls = []
                current_url = RTFM_PAGE_TYPES[self.current_page_type]
                link_pattern = re.compile(r'(?<=#)([^#\s]+)')
                for key, value in matches:
                    value = re.sub(link_pattern, fr'[{STYLE_LINK}]\1[/{STYLE_LINK}]', value)
                    match_urls.append(f'{current_url}/{value}')
                match_urls = '\n'.join(match_urls)
                self.console_output['docs_links'] = Panel.fit(match_urls,
                                                    border_style=STYLE_BORDER,
                                                    style=STYLE_LINK_ID,
                                                    subtitle=f"matches for '{input}'")
                
    def main(self):
        try:
            while True:
                self.console.print("Enter a command or 'quit' to quit", style=STYLE_GENERAL)
                command = input('> ').lower()
                self.tree = Tree('', guide_style='bold', hide_root=True)
                if command == 'refresh':
                    self.run(refresh_cache=True)
                    continue
                elif command in RTFM_PAGE_TYPES:
                    if command in self.rtfm_cache:
                        self.current_page_type = command
                        self.console.print(f"Switched page type to: '{command}'", style=STYLE_NOTICE)
                    else:
                        self.console.print(f"Unable to switch to '{command}' as the local cache is not available.",
                                           style=STYLE_WARNING)
                    continue
                elif command == 'mode':
                    self.render_tree = not self.render_tree
                    render_mode = 'tree' if self.render_tree else 'links'
                    self.console.print(f"Render mode changed to: '{render_mode}'", style=STYLE_NOTICE)
                    continue
                elif command == 'quit':
                    break
                else:
                    self.do_rtfm(command)

                if self.console_output['docs_tree']:
                    self.console.print(self.console_output['docs_tree'])
                elif self.console_output['docs_links']:
                    self.console.print(self.console_output['docs_links'])
                else:
                    self.console.print('No results for your query.', style=STYLE_WARNING)
        except KeyboardInterrupt:
            pass

if __name__ == '__main__':
    rtfm = RTFM()
    rtfm.main()
