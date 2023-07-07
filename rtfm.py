""" Based on:
https://github.com/Rapptz/RoboDanny/blob/rewrite/cogs/api.py
"""
import re
import zlib
import io
import json
import os
import httpx

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.tree import Tree
from deepdiff import DeepDiff
from rich.traceback import install
install(show_locals=False)

from typing import Callable, Generator, Optional, Iterable, TypeVar

CURRENT_PAGE_TYPE = 'stable'
RTFM_PAGE_TYPES = {
    'stable': 'https://discordpy.readthedocs.io/en/stable',
    'latest': 'https://discordpy.readthedocs.io/en/latest',
    'python': 'https://docs.python.org/3',
}
DATA_DIRECTORY = os.getcwd()

T = TypeVar('T')
# https://github.com/Rapptz/RoboDanny/blob/rewrite/cogs/utils/fuzzy.py#L325-L350
def finder(
    text: str,
    collection: Iterable[T],
    *,
    key: Optional[Callable[[T], str]] = None,
    raw: bool = False,
) -> list[tuple[int, T]] | list[T]:
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

    if raw:
        return sorted(suggestions, key=sort_key)
    else:
        return [z for _, z in sorted(suggestions, key=sort_key)]

class SphinxObjectFileReader:
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
    def __init__(self):
        self.rtfm_cache: dict = {}
        self.console_output = {'notice': [], 'warning': [], 'docs_links': [], 'cache_diff': {}}
        self.current_page_type = CURRENT_PAGE_TYPE
        self.current_url = RTFM_PAGE_TYPES[CURRENT_PAGE_TYPE]
        
        self.build_rtfm_lookup_table()
    
    def parse_object_inv(self, stream: SphinxObjectFileReader, url: str) -> dict[str, str]:
        # key: URL
        # n.b.: key doesn't have `discord` or `discord.ext.commands` namespaces
        result: dict[str, str] = {}

        # first line is version info
        inv_version = stream.readline().rstrip()

        if inv_version != '# Sphinx inventory version 2':
            raise RuntimeError('Invalid objects.inv file version.')

        # next line is "# Project: <name>"
        # then after that is "# Version: <version>"
        projname = stream.readline().rstrip()[11:]
        version = stream.readline().rstrip()[11:]

        # next line says if it's a zlib header
        line = stream.readline()
        if 'zlib' not in line:
            raise RuntimeError('Invalid objects.inv file, not z-lib compatible.')

        # This code mostly comes from the Sphinx repository.
        entry_regex = re.compile(r'(?x)(.+?)\s+(\S*:\S*)\s+(-?\d+)\s+(\S+)\s+(.*)')
        for line in stream.read_compressed_lines():
            match = entry_regex.match(line.rstrip())
            if not match:
                continue

            name, directive, prio, location, dispname = match.groups()
            domain, _, subdirective = directive.partition(':')
            if directive == 'py:module' and name in result:
                continue

            if directive == 'std:doc' or \
                subdirective == 'label':
                #subdirective = 'label'
                continue

            if location.endswith('$'):
                location = location[:-1] + name

            key = name if dispname == '-' else dispname
            prefix = f'{subdirective}:' if domain == 'std' else ''

            if projname == 'discord.py':
                key = key.replace('discord.ext.commands.', '').replace('discord.', '')

            result[f'{prefix}{key}'] = os.path.join(url, location)

        return result
    
    def save_cache(self, cache_name: str, cache: dict) -> None:
        # RoboDanny is OP and stays on for weeks at a time.
        # Since this is CLI version of `?rtfm` that will likely be opened 
        # and closed 5 times an hour, we write the cache to a file.
        #cache_file = {}
        #cache_diff = {} TODO: add diffing on cache update
        #original_cache = {}
        #if cache_name in os.listdir(DATA_DIRECTORY):
        #    with open(os.path.join(DATA_DIRECTORY, cache_name), 'r') as fp:
        #        original_cache = json.load(fp) # read original
        with open(os.path.join(DATA_DIRECTORY, cache_name), 'w') as fp:
            json.dump(cache, fp)
        #with open(os.path.join(DATA_DIRECTORY, cache_name), 'r') as fp:
        #    cache_file = json.load(fp) # read new
        #    cache_diff = self.get_diff(cache_file, original_cache) # compare using DeepDiff
        #    self.console_output['cache_diff'] = cache_diff

    def get_diff(self, old: dict, new: dict):
        return DeepDiff(old, new, ignore_order=True, report_repetition=True, view='tree')

    def build_rtfm_lookup_table(self):
        cache: dict[str, dict[str, str]] = {}
        with httpx.Client() as session:
            for key, page in RTFM_PAGE_TYPES.items():
                cache[key] = {}
                response = session.get(page + '/objects.inv')
                
                if response.status_code  != 200:
                    self.console_output['warning'].append(f"Got status code {response.status_code} when downloading {page + '/objects.inv'}")
                    continue

                content: bytes = response.content

                stream = SphinxObjectFileReader(content)
                cache[key] = self.parse_object_inv(stream, page)

                for key, value in cache.items():
                    self.rtfm_cache[key] = value
                    key += '_cache.json'
                    self.save_cache(key, value)

    def do_rtfm(self, obj):
        obj = re.sub(r'^(?:discord\.(?:ext\.)?)?(?:commands\.)?(.+)', r'\1', obj)
        cache = list(self.rtfm_cache[self.current_page_type].items())
        matches = finder(obj, cache, key=lambda t: t[0])[:8]

        nodes = {}

        for key, value in matches:
            parts = key.split('.') # type: ignore
            for i in range(len(parts)):
                current_hierarchy = '.'.join(parts[:i+1])

                if current_hierarchy not in nodes:
                    parent_hierarchy = '.'.join(parts[:i])
                    parent_node = nodes[parent_hierarchy] if parent_hierarchy in nodes else self.tree
                    nodes[current_hierarchy] = parent_node.add(
                            f'[link={value}]{parts[i]}[/link]'
                        )

        self.console_output['docs_tree'] = Panel.fit(self.tree)
        return 

    def main(self):
        console = Console()
        while True:
            console.print('Enter a command or `quit` to quit', style='bold yellow')
            command = input('> ').lower()

            self.tree = Tree('', guide_style='bold', hide_root=True)

            if command == 'refresh cache':
                self.build_rtfm_lookup_table()
                continue
            elif command in ('stable', 'latest'):
                self.current_page_type = command
                console.print(f'Switched page type to: `{command}`')
                continue
            elif command == 'quit':
                break
            else:
                self.do_rtfm(command)

            if self.console_output['cache_diff']:
                console.print(
                            Panel(
                                self.console_output['cache_diff'],
                                box=box.ASCII2
                            ))

            elif self.console_output['docs_tree']:

                console.print(self.console_output['docs_tree'])

            else:
                console.print('No results for your query.')

            for line in self.console_output['notice']:
                console.print(line)

if __name__ == '__main__':
    rtfm = RTFM()
    rtfm.main()
    