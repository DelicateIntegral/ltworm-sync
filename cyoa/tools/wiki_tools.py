import itertools
import itertools
import operator
import os
import sys
from concurrent.futures import as_completed
from concurrent.futures.thread import ThreadPoolExecutor
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, TextIO

import jinja2
import requests
from lenses import lens
from rich.progress import Progress, TextColumn, BarColumn, TimeRemainingColumn, MofNCompleteColumn

from cyoa.tools.lib import ToolBase, ProjectUtilsMixin, console
from cyoa.tools.wiki.config import STRUCTURE

URL_BASE = "https://cyoa.ltouroumov.ch/wiki/api.php"


def batched(iterable, n):
    # batched('ABCDEFG', 3) --> ABC DEF G
    if n < 1:
        raise ValueError('n must be at least one')
    it = iter(iterable)
    while batch := tuple(itertools.islice(it, n)):
        yield batch


class WikiError(BaseException):
    def __init__(self, code, info):
        self.code = code
        self.info = info
        super().__init__(f"{code}: {info}")


class WikiAPI:
    def __init__(self):
        self.session = requests.Session()

    def _get_token(self, token_type):
        tokens_res = self.session.get(URL_BASE, params={
            "action": "query",
            "meta": "tokens",
            "type": token_type,
            "format": "json"
        })
        token_res_json = tokens_res.json()
        tokens_map: dict = token_res_json['query']['tokens']
        tokens = list(tokens_map.values())
        return tokens[0]

    def login(self, username, password):
        login_token = self._get_token(token_type="login")
        console.print(f"Login as {username}")

        res = self.session.post(URL_BASE, data={
            "action": "login",
            "lgname": username,
            "lgpassword": password,
            "lgtoken": login_token,
            "format": "json",
        }).json()

        if res['login']['result'] != 'Success':
            console.log(res)
            raise Exception('Failed to login')

    def _query_raw(self, **kwargs):
        query_res = self.session.get(URL_BASE, params={
            "action": "query", "format": "json",
            **kwargs
        })

        return query_res.json()

    def query_prefix(self, prefix: str, **kwargs):
        can_continue = True
        do_continue = None
        while can_continue:
            query_res = self._query_raw(
                list="allpages",
                apprefix=prefix,
                apcontinue=do_continue,
                **kwargs
            )

            if 'query' in query_res:
                yield from query_res['query']['allpages']
            if 'continue' in query_res:
                do_continue = query_res['continue']['apcontinue']
            else:
                can_continue = False

    def query_content(self, **kwargs):
        query_res = self._query_raw(prop="revisions", rvslots="*", rvprop="ids|content", **kwargs)
        return query_res['query']['pages']

    def parse(self, **kwargs):
        query_res = self.session.get(URL_BASE, params={
            "action": "parse", "format": "json",
            **kwargs
        })

        console.print(query_res.json())

    def edit(self, **kwargs):
        csrf_token = self._get_token(token_type='csrf')

        query_res = self.session.post(URL_BASE, data={
            "action": "edit", "format": "json",
            **kwargs,
            "token": csrf_token,
        }).json()

        if error := query_res.get('error', None):
            raise WikiError(code=error['code'], info=error['info'])
        else:
            return query_res['edit']


def get_last_revision(wiki_page):
    return wiki_page['revisions'][0]['slots']['main']['*']


def matches_last_revision(wiki_page, page_text):
    if not wiki_page:
        return False

    wiki_text = get_last_revision(wiki_page)
    return wiki_text == page_text


def make_marker(marker_start, marker_end):
    def mark(text):
        return f"{marker_start}\n{text}\n{marker_end}"

    return mark


def splice_text(wiki_page, page_text, marker):
    marker_start = f'<!-- {marker} -->'
    marker_end = f'<!-- /{marker} -->'
    mark = make_marker(marker_start, marker_end)

    if not wiki_page:
        return mark(page_text)

    wiki_text = get_last_revision(wiki_page)
    section_start = wiki_text.find(marker_start)
    section_end = wiki_text.find(marker_end)

    if section_start < 0 or section_end < 0 or section_end < section_start:
        # Squash page contents for now
        return f"{mark(page_text)}"
    elif section_start == 0:
        # Splice from the start
        return f"{mark(page_text)}{wiki_text[section_end + len(marker_end):]}"
    else:
        return f"{wiki_text[:section_start]}{mark(page_text)}{wiki_text[section_end + len(marker_end):]}"


def check_title(title: str):
    return (
        title
        .replace('/', '\uFF0F')
        .replace('[', '')
        .replace(']', '')
        .strip()
    )


def remove_indent(text: str) -> str:
    lines = text.split('\n')
    return str.join('\n', (
        str.lstrip(line)
        for line in lines
    ))


def get_depth(path: str):
    return len(path.split('/'))


def get_index_order(title: str, structure):
    ORDER_LENS = lens.Get(title, {}).Get('index', {}).Get('order', -1).get()
    return ORDER_LENS(structure)


def last_title_part(title: str):
    slash_idx = title.rfind('/')
    if slash_idx >= 0:
        return title[slash_idx + 1:]
    else:
        return title


def child_pages_of(pages: list, path: str, search_depth: int = 1):
    cur_depth = get_depth(path)
    return list(filter(
        lambda item: (item['title'] != path and
                      str.startswith(item['title'], path) and
                      (get_depth(item['title']) - cur_depth) <= search_depth),
        pages,
    ))


class WikiUpdateTool(ToolBase, ProjectUtilsMixin):
    name = 'wiki.update'

    api: WikiAPI
    env: jinja2.Environment
    rows: dict[str, Any]
    objects: dict[str, Any]
    scores: dict[str, Any]

    @classmethod
    def setup_parser(cls, parent):
        parser = parent.add_parser(cls.name, help='Format a project file')
        parser.add_argument('--project', dest='project_file', type=Path)
        parser.add_argument('--preview', action='store_true')
        parser.add_argument('--filter-path', default='*')
        parser.add_argument('--filter-type', default='section,index,combined')
        parser.add_argument('--bot-username')
        parser.add_argument('--bot-password')

    def setup(self, args):
        self.api = WikiAPI()
        self.api.login(
            args.bot_username,
            args.bot_password
        )

        tpl_path = os.path.join(os.path.dirname(__file__), 'wiki')
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(tpl_path),
            undefined=jinja2.ChainableUndefined
        )

        self.rows = {
            row['id']: row
            for row in self.project['rows']
        }

        self.objects = {
            obj['id']: obj
            for row in self.rows.values()
            for obj in row['objects']
        }

        self.scores = {
            score['id']: score
            for score in self.project['pointTypes']
        }

        self.env.filters['last_title_part'] = last_title_part
        self.env.filters['child_pages_of'] = child_pages_of
        self.env.globals = {
            "rows": self.rows,
            "objects": self.objects,
            "scores": self.scores,
        }

    def run(self, args):
        self._load_project(args.project_file)
        self.setup(args)

        css_file_path = os.path.join(os.path.dirname(__file__), "wiki/custom.css")
        with open(css_file_path, mode='r') as css_file:
            self.api.edit(title="MediaWiki:Common.css",
                          text=css_file.read())

            console.print('Updated: CSS')

        structure_items = filter(
            lambda tup: (
                fnmatch(tup[0], args.filter_path) and
                tup[1]['mode'] in args.filter_type
            ),
            STRUCTURE.items()
        )

        leaf_nodes = []
        index_nodes = []
        for path, node in structure_items:
            if node['mode'] == 'section':
                leaf_nodes.append((path, node))
            else:
                index_nodes.append((path, node))

        columns = [
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
            TimeRemainingColumn(),
        ]
        with (Progress(*columns) as p,
              ThreadPoolExecutor() as ex):
            total_nodes = len(leaf_nodes) + len(index_nodes)
            _main = p.add_task("Sections", total=total_nodes)

            leaf_futures = [
                ex.submit(self.build_section, path, config, args, p)
                for path, config in leaf_nodes
            ]
            for _fut in as_completed(leaf_futures):
                _fut.result()
                p.advance(_main)

            index_ordered = reversed(sorted(index_nodes, key=operator.itemgetter(0)))
            for path, config in index_ordered:
                self.build_section(path, config, args, p)
                p.advance(_main)

    def collect_index(self, path, config, p):
        # p.console.print(f'collecting: {path} ...')
        cur_depth = get_depth(path)
        search_depth = config['index']['depth']
        # Search for sub-pages in structure
        wiki_page_names = filter(
            lambda item: (item != path and
                          str.startswith(item, path) and
                          (get_depth(item) - cur_depth) <= search_depth),
            STRUCTURE.keys(),
        )
        # Fetch page data (batched)
        wiki_pages = [
            self.api.query_content(titles=str.join('|', batch))
            for batch in batched(wiki_page_names, 20)
        ]
        # Flatten batches
        wiki_pages = itertools.chain.from_iterable(
            batch.values() for batch in wiki_pages
        )
        # Sort pages by order
        wiki_pages = sorted(
            wiki_pages,
            key=lambda item: (
                get_index_order(item['title'], STRUCTURE),
                item['title']
            )
        )
        return list(wiki_pages)

    def collect_pages(self, path, config, args, p):
        # p.console.print(f'loading: {path} ...')
        wiki_pages: list = list(self.api.query_prefix(path))
        if len(wiki_pages) > 0:
            wiki_pages: dict = self.api.query_content(pageids=str.join('|', [
                f"{wiki_page['pageid']}"
                for wiki_page in wiki_pages
            ]))
            wiki_pages: dict = {
                wiki_page['title']: wiki_page
                for wiki_page in wiki_pages.values()
            }
        else:
            wiki_pages: dict = {}

        # p.console.print(f'pages: {len(wiki_pages.keys())}')

        _objects = p.add_task(
            f'Section {last_title_part(path)}',
            total=sum((len(self.rows[row_id]['objects']) for row_id in config['row_ids']))
        )
        page_rows = []
        for row_id in config['row_ids']:
            row_data = self.rows[row_id]
            row_pages = []
            skipped_count = 0
            for obj_data in row_data['objects']:
                page_title = check_title(obj_data['title'])
                page_name = f"{path}/{page_title}"
                wiki_page = wiki_pages.get(page_name, None)

                page_tpl = self.env.get_template('object.tpl')
                page_text = remove_indent(page_tpl.render(
                    page_name=page_name,
                    row=row_data,
                    obj=obj_data
                ))
                page_text = splice_text(
                    wiki_page,
                    page_text,
                    marker=f'SYNC:{obj_data["id"]}'
                )

                if args.preview:
                    console.print(page_text)

                if not matches_last_revision(wiki_page, page_text):
                    self.api.edit(title=page_name,
                                  text=page_text)

                    p.console.print(f'updated page: {page_name} (new: {wiki_page is None})')
                else:
                    skipped_count += 1

                row_pages.append({
                    "name": page_name,
                    "obj": row_data,
                })
                p.advance(_objects)

            # p.console.print(f'skipped: {skipped_count}')
            page_rows.append({
                "row": row_data,
                "objects": row_pages
            })

        p.remove_task(_objects)
        return page_rows

    def build_section(self, path, config, args, p):
        # p.console.print(f'section: {path}')
        # console.print_json(data=config)

        if config['mode'] in ('index', 'combined'):
            wiki_pages = self.collect_index(path, config, p)
        else:
            wiki_pages = []

        if config['mode'] in ('section', 'combined'):
            page_rows = self.collect_pages(path, config, args, p)
        else:
            page_rows = []

        _pages = self.api.query_content(titles=path)
        if len(_pages) > 0:
            index_wiki = list(_pages.values())[0]
        else:
            index_wiki = None

        index_tpl = self.env.get_template('section.tpl')
        index_text = remove_indent(index_tpl.render(
            page_path=path,
            page_rows=page_rows,
            pages=wiki_pages,
            config=config
        ))

        if args.preview:
            console.print(index_text)

        if not matches_last_revision(index_wiki, index_text):
            self.api.edit(title=path,
                          text=index_text)

            p.console.print(f'updated index: {path}')


class WikiPowersIndexTool(ToolBase, ProjectUtilsMixin):
    name = 'wiki.rows-table'

    @classmethod
    def setup_parser(cls, parent):
        parser = parent.add_parser(cls.name, help='Format a project file')
        parser.add_argument('--project', dest='project_file',
                            type=Path, required=True)
        parser.add_argument('--rows', dest='rows',
                            action='extend', nargs='+',
                            type=str, required=True)
        parser.add_argument('--output', dest='output_file',
                            type=Path, default=None)

    def run(self, args):
        self._load_project(args.project_file)

        if args.output_file:
            output: TextIO = open(args.output_file, mode='w+')
        else:
            output: TextIO = sys.stdout

        def nl(ln: str):
            return f"{ln}\n"

        for row_data in self.project['rows']:
            if row_data['id'] not in args.rows:
                continue

            output.writelines(map(nl, [
                f'== {row_data["title"]} ==',
                f'{{| class="wikitable"',
                f'|+',
                f'!ID',
                f'!Object / Addon',
                f'!Author',
            ]))

            for obj_data in row_data['objects']:
                addon_count = len(obj_data['addons'])
                output.writelines(map(nl, [
                    f'|- style="vertical-align:top;"',
                    f'|rowspan="{addon_count + 1}"|{obj_data["id"]}',
                    f'|{obj_data["title"]}',
                    f'|???'
                ]))

                for idx, obj_addon in enumerate(obj_data['addons']):
                    output.writelines(map(nl, [
                        f'|- style="vertical-align:top;"',
                        f'|\'\'Addon\'\': {obj_addon["title"]}',
                        f'|???'
                    ]))

            output.write('|}\n')


TOOLS = (
    WikiUpdateTool,
    WikiPowersIndexTool,
)
