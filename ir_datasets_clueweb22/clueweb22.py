from contextlib import contextmanager, ExitStack
from csv import reader
from datetime import datetime
from enum import Enum
from gzip import open as gzip_open
from io import TextIOWrapper
from itertools import groupby
from json import loads
from os import PathLike
from os.path import join
from pathlib import Path
from typing import Collection, NamedTuple, Sequence, Optional, Type, Iterator, IO, Iterable, Mapping, Union, AbstractSet, Tuple, ContextManager, Callable
from uuid import UUID
from zipfile import ZipFile

from fastwarc import ArchiveIterator, WarcRecordType
from ir_datasets import registry
from ir_datasets.datasets.base import Dataset
from ir_datasets.formats import BaseDocs
from ir_datasets.indices import Docstore
from ir_datasets.log import easy
from ir_datasets.util import Download, apply_sub_slice, slice_idx, home_path
from ir_datasets_clueweb22.io import OffsetIOWrapper
from ir_datasets_clueweb22.util import DownloadConfig, YamlDocumentation


# Logging.
_logger = easy("clueweb22")

# Constants and constraints.
_MAX_SUBDIRECTORIES_PER_STREAM: int = 80
_MAX_FILES_PER_SUBDIRECTORY: int = 100
_ENCODING = "utf8"


# Base records corresponding to the file types listed
# at https://lemurproject.org/clueweb22/docspecs.php
class _Txt(NamedTuple):
    """
    Record from the ``txt`` subdir.
    """
    doc_id: str
    url: str
    url_hash: str
    language: str
    text: str


class AnnotationType(Enum):
    NONE = "None"
    PRIMARY = "Primary"
    HEADING = "Heading"
    TITLE = "Title"
    PARAGRAPH = "Paragraph"
    TABLE = "Table"
    LIST = "List"


class _Html(NamedTuple):
    """
    Record from the ``html`` subdirectory.
    """
    doc_id: str
    url: str
    url_hash: str
    language: str
    date: datetime
    record_id: UUID
    payload_digest: str
    html: bytes
    vdom_nodes: Mapping[AnnotationType, Sequence[int]]


class Anchor(NamedTuple):
    """
    Anchor sub-record from the ``inlink`` and ``outlink`` subdirectories.
    """
    url: str
    url_hash: str
    text: str
    language: str


class _Link(NamedTuple):
    """
    Record from the ``inlink`` and ``outlink`` subdirectories.
    """
    doc_id: str
    url: str
    url_hash: str
    anchors: Sequence[Anchor]


class _Vdom(NamedTuple):
    """
    Record from the ``vdom`` subdirectory.
    """
    # Not parsed yet because parsing would require the protobuf library.
    vdom: bytes


class _Jpg(NamedTuple):
    """
    Record from the ``jpg`` subdirectory.
    """
    screenshot: bytes


_Record = _Txt | _Html | _Link | _Vdom | _Jpg | None


# Readers for parsing the base record types from iterators of IO streams.
def _read_txt(file: IO[bytes]) -> Iterator[_Txt]:
    with TextIOWrapper(file, encoding=_ENCODING) as text_file:
        for line in text_file:
            json = loads(line)
            url = json["URL"]
            if url.endswith("\n"):
                url = url[:-1]  # Strip trailing newline.
            yield _Txt(
                doc_id=json["ClueWeb22-ID"],
                # Bug:
                # URLs from txt records contain an additional new line (\n)
                # at the end of the URL but the other records don't.
                url=url,
                url_hash=json["URL-hash"],
                language=json["Language"],
                text=json["Clean-Text"],
            )


def _read_html(file: IO[bytes]) -> Iterator[_Html]:
    for document in ArchiveIterator(
            file,
            record_types=WarcRecordType.response,
            parse_http=False,
    ):
        doc_id = document.headers["ClueWeb22-ID"]
        url = document.headers['WARC-Target-URI']
        url_hash = document.headers["URL-Hash"]
        language = document.headers["Language"]
        date_header = document.headers["WARC-Date"]
        date_format: str
        if "." in date_header:
            date_format = "%Y-%m-%dT%H:%M:%S.%fZ"
        else:
            date_format = "%Y-%m-%dT%H:%M:%SZ"
        date = datetime.strptime(date_header, date_format)
        record_id = UUID(document.headers["WARC-Record-ID"][1:-1])
        payload_digest = document.headers["WARC-Payload-Digest"]
        content_length = int(document.headers["Content-Length"])
        html: bytes = document.reader.read()
        assert len(html) == content_length  # nosec: B101
        vdom_nodes = {
            annotation_type: [
                int(vdom)
                for vdom in document.headers.get(
                    f"VDOM-{annotation_type.value}", ""
                ).split()
            ]
            for annotation_type in AnnotationType
        }
        yield _Html(
            doc_id=doc_id,
            url=url,
            url_hash=url_hash,
            language=language,
            date=date,
            record_id=record_id,
            payload_digest=payload_digest,
            html=html,
            vdom_nodes=vdom_nodes,
        )


def _parse_anchor(json: Sequence[str]) -> Anchor:
    return Anchor(
        url=json[0],
        url_hash=json[1],
        text=json[2],
        language=json[4],
    )


def _read_inlink(file: IO[bytes]) -> Iterator[Optional[_Link]]:
    with TextIOWrapper(file, encoding=_ENCODING) as text_file:
        for line in text_file:
            if len(line.strip()) == 0:
                yield None
                continue
            json = loads(line)
            yield _Link(
                doc_id=json["ClueWeb22-ID"],
                url=json["url"],
                url_hash=json["urlhash"],
                anchors=[
                    _parse_anchor(anchor) for anchor in json["anchors"]
                ],
            )


def _read_outlink(file: IO[bytes]) -> Iterator[Optional[_Link]]:
    with TextIOWrapper(file, encoding=_ENCODING) as text_file:
        for line in text_file:
            if len(line.strip()) == 0:
                yield None
                continue
            json = loads(line)
            yield _Link(
                doc_id=json["ClueWeb22-ID"],
                url=json["url"],
                url_hash=json["urlhash"],
                anchors=[
                    _parse_anchor(anchor) for anchor in json["outlinks"]
                ],
            )


def _read_vdom(file: IO[bytes]) -> Iterator[_Vdom]:
    yield _Vdom(file.read())


def _read_jpg(file: IO[bytes]) -> Iterator[_Jpg]:
    yield _Jpg(file.read())


# Doc records corresponding to the fields available for each subset listed
# at https://lemurproject.org/clueweb22/obtain.php and in the SIRIP paper.


class ClueWeb22LDoc(NamedTuple):
    doc_id: str
    url: str
    url_hash: str
    language: str
    text: str

    def default_text(self):
        return self.text


class ClueWeb22ADoc(NamedTuple):
    doc_id: str
    url: str
    url_hash: str
    language: str
    text: str
    date: datetime
    html: bytes
    record_id: UUID
    payload_digest: str
    vdom_nodes: Mapping[AnnotationType, Sequence[int]]
    vdom: bytes
    inlink_anchors: Sequence[Anchor]
    outlink_anchors: Sequence[Anchor]

    def default_text(self):
        return self.text


class ClueWeb22BDoc(NamedTuple):
    doc_id: str
    url: str
    url_hash: str
    language: str
    text: str
    date: datetime
    html: bytes
    record_id: UUID
    payload_digest: str
    vdom_nodes: Mapping[AnnotationType, Sequence[int]]
    vdom: bytes
    inlink_anchors: Sequence[Anchor]
    outlink_anchors: Sequence[Anchor]
    # TODO Enable screenshot once JPGs are released.
    # screenshot: bytes

    def default_text(self):
        return self.text


_Doc = ClueWeb22LDoc | ClueWeb22ADoc | ClueWeb22BDoc


# Combining iterators to construct documents from base records.from
def _combine_l_docs(
        record_iterators: Sequence[Iterator[_Record]],
) -> Iterator[ClueWeb22LDoc]:
    zipped = zip(*record_iterators)
    txt: _Txt
    for txt, in zipped:
        yield ClueWeb22LDoc(
            doc_id=txt.doc_id,
            url=txt.url,
            url_hash=txt.url_hash,
            language=txt.language,
            text=txt.text,
        )


def _combine_a_docs(
        record_iterators: Sequence[Iterator[_Record]],
) -> Iterator[ClueWeb22ADoc]:
    zipped = zip(*record_iterators)
    txt: _Txt
    html: _Html
    inlink: _Link
    outlink: _Link
    vdom: _Vdom
    for txt, html, inlink, outlink, vdom in zipped:
        assert txt.doc_id == html.doc_id  # nosec: B101
        if txt.url != html.url:
            # Bug:
            # URLs from txt records are truncated to everything
            # before the first comma (,).
            # Example: For clueweb22-de0000-00-00366, the html URL
            # is https://www.anisearch.de/manga/43556,verrueckt-nach-dir but
            # the txt URL hash is just https://www.anisearch.de/manga/43556.
            _logger.debug(
                f"URL mismatch for {txt.doc_id}: "
                f"txt URL was {txt.url} but "
                f"html URL was {html.url}"
            )
            assert "," in html.url and html.url.split(
                ",")[0] == txt.url  # nosec: B101
        if txt.url_hash != html.url_hash:
            # Bug:
            # Sometimes, URL hashes from txt records do not match the
            # corresponding URL hashes from other records.
            # Example: For clueweb22-de0000-00-13406, the html URL hash
            # is B6956297B5EBBDFEAABF458F2FA5EADC but the txt URL hash
            # is 9D5A53C6ACCB07B2C2319A4E5E44AB76.
            _logger.warn(
                f"URL hash mismatch for {txt.doc_id}: "
                f"txt URL hash was {txt.url_hash} but "
                f"html URL hash was {html.url_hash}"
            )
        assert txt.language == "other" or txt.language == html.language  # nosec: B101
        if inlink is not None:
            assert inlink.doc_id == html.doc_id  # nosec: B101
            if inlink.url != html.url:
                # Bug:
                # Sometimes, URLs from inlink records do not match the
                # corresponding URLs from other records.
                # Example: For clueweb22-de0000-01-14834, the html URL
                # is https://simon-transporte.com/ but the inlink URL
                # is https://simon.ccbcmd.edu/pls/PROD/bwskalog.p_disploginnew?in_id=&cpbl=&newid=.
                _logger.warn(
                    f"URL mismatch for {html.doc_id}: "
                    f"inlink URL was {inlink.url} but "
                    f"html URL was {html.url}"
                )
            if inlink.url_hash != html.url_hash:
                # Bug:
                # Sometimes, URL hashes from inlink records do not match the
                # corresponding URL hashes from other records.
                # Example: For clueweb22-de0000-01-14834, the html URL hash
                # is 825E120CE7F82C8B0268440A59107D04 but the inlink URL hash
                # is 612691A107701D76AD36FD32F8608F3C.
                _logger.warn(
                    f"URL hash mismatch for {txt.doc_id}: "
                    f"inlink URL hash was {txt.url_hash} but "
                    f"html URL hash was {html.url_hash}"
                )
        if outlink is not None:
            assert outlink.doc_id == html.doc_id  # nosec: B101
            if outlink.url != html.url:
                # Bug:
                # Sometimes, URLs from outlink records do not match the
                # corresponding URLs from other records.
                # Example: For clueweb22-de0000-00-13406, the html URL
                # is https://www.jovanna.de/ but the outlink URL
                # is https://www.jovanovic.com/quotidien.htm.
                _logger.warn(
                    f"URL mismatch for {html.doc_id}: "
                    f"outlink URL was {outlink.url} but "
                    f"html URL was {html.url}"
                )
            if outlink.url_hash != html.url_hash:
                # Bug:
                # Sometimes, URL hashes from outlink records do not match the
                # corresponding URL hashes from other records.
                # Example: For clueweb22-de0000-00-13406, the html URL hash
                # is B6956297B5EBBDFEAABF458F2FA5EADC but the outlink URL hash
                # is 9D5A53C6ACCB07B2C2319A4E5E44AB76.
                _logger.warn(
                    f"URL hash mismatch for {txt.doc_id}: "
                    f"outlink URL hash was {txt.url_hash} but "
                    f"html URL hash was {html.url_hash}"
                )
        yield ClueWeb22ADoc(
            doc_id=html.doc_id,
            url=html.url,
            url_hash=html.url_hash,
            language=html.language,
            text=txt.text,
            date=html.date,
            html=html.html,
            record_id=html.record_id,
            payload_digest=html.payload_digest,
            vdom_nodes=html.vdom_nodes,
            vdom=vdom.vdom,
            inlink_anchors=inlink.anchors if inlink is not None else [],
            outlink_anchors=outlink.anchors if outlink is not None else [],
        )


def _combine_b_docs(
        record_iterators: Sequence[Iterator[_Record]],
) -> Iterator[ClueWeb22BDoc]:
    zipped = zip(*record_iterators)
    # TODO Enable screenshot once JPGs are released.
    # for txt, html, inlink, outlink, vdom, jpg in zipped:
    for txt, html, inlink, outlink, vdom in zipped:
        assert txt.doc_id == html.doc_id  # nosec: B101
        if txt.url != html.url:
            # Bug:
            # URLs from txt records are truncated to everything
            # before the first comma (,).
            # Example: For clueweb22-de0000-00-00366, the html URL
            # is https://www.anisearch.de/manga/43556,verrueckt-nach-dir but
            # the txt URL hash is just https://www.anisearch.de/manga/43556.
            _logger.debug(
                f"URL mismatch for {txt.doc_id}: "
                f"txt URL was {txt.url} but "
                f"html URL was {html.url}"
            )
            assert "," in html.url and html.url.split(
                ",")[0] == txt.url  # nosec: B101
        if txt.url_hash != html.url_hash:
            # Bug:
            # Sometimes, URL hashes from txt records do not match the
            # corresponding URL hashes from other records.
            # Example: For clueweb22-de0000-00-13406, the html URL hash
            # is B6956297B5EBBDFEAABF458F2FA5EADC but the txt URL hash
            # is 9D5A53C6ACCB07B2C2319A4E5E44AB76.
            _logger.warn(
                f"URL hash mismatch for {txt.doc_id}: "
                f"txt URL hash was {txt.url_hash} but "
                f"html URL hash was {html.url_hash}"
            )
        assert txt.language == "other" or txt.language == html.language  # nosec: B101
        if inlink is not None:
            assert inlink.doc_id == html.doc_id  # nosec: B101
            if inlink.url != html.url:
                # Bug:
                # Sometimes, URLs from inlink records do not match the
                # corresponding URLs from other records.
                # Example: For clueweb22-de0000-01-14834, the html URL
                # is https://simon-transporte.com/ but the inlink URL
                # is https://simon.ccbcmd.edu/pls/PROD/bwskalog.p_disploginnew?in_id=&cpbl=&newid=.
                _logger.warn(
                    f"URL mismatch for {html.doc_id}: "
                    f"inlink URL was {inlink.url} but "
                    f"html URL was {html.url}"
                )
            if inlink.url_hash != html.url_hash:
                # Bug:
                # Sometimes, URL hashes from inlink records do not match the
                # corresponding URL hashes from other records.
                # Example: For clueweb22-de0000-01-14834, the html URL hash
                # is 825E120CE7F82C8B0268440A59107D04 but the inlink URL hash
                # is 612691A107701D76AD36FD32F8608F3C.
                _logger.warn(
                    f"URL hash mismatch for {txt.doc_id}: "
                    f"inlink URL hash was {txt.url_hash} but "
                    f"html URL hash was {html.url_hash}"
                )
        if outlink is not None:
            assert outlink.doc_id == html.doc_id  # nosec: B101
            if outlink.url != html.url:
                # Bug:
                # Sometimes, URLs from outlink records do not match the
                # corresponding URLs from other records.
                # Example: For clueweb22-de0000-00-13406, the html URL
                # is https://www.jovanna.de/ but the outlink URL
                # is https://www.jovanovic.com/quotidien.htm.
                _logger.warn(
                    f"URL mismatch for {html.doc_id}: "
                    f"outlink URL was {outlink.url} but "
                    f"html URL was {html.url}"
                )
            if outlink.url_hash != html.url_hash:
                # Bug:
                # Sometimes, URL hashes from outlink records do not match the
                # corresponding URL hashes from other records.
                # Example: For clueweb22-de0000-00-13406, the html URL hash
                # is B6956297B5EBBDFEAABF458F2FA5EADC but the outlink URL hash
                # is 9D5A53C6ACCB07B2C2319A4E5E44AB76.
                _logger.warn(
                    f"URL hash mismatch for {txt.doc_id}: "
                    f"outlink URL hash was {txt.url_hash} but "
                    f"html URL hash was {html.url_hash}"
                )
        yield ClueWeb22BDoc(
            doc_id=html.doc_id,
            url=html.url,
            url_hash=html.url_hash,
            language=html.language,
            text=txt.text,
            date=html.date,
            html=html.html,
            record_id=html.record_id,
            payload_digest=html.payload_digest,
            vdom_nodes=html.vdom_nodes,
            vdom=vdom.vdom,
            inlink_anchors=inlink.anchors if inlink is not None else [],
            outlink_anchors=outlink.anchors if outlink is not None else [],
            # TODO Enable screenshot once JPGs are released.
            # screenshot=jpg.jpg,
        )


# Type-safe configuration for the subsets, describing the characteristics
# of files and compression types.


class ClueWeb22Compression(Enum):
    GZIP = 1
    ZIP = 2


_FormatReader = Callable[[IO[bytes]], Iterator[_Record]]
"""
Function for reading records from the decompressed files.
"""


class _FormatInfo(NamedTuple):
    id: str
    extension: str
    offset_extension: Optional[str]
    compression: ClueWeb22Compression
    compression_extension: Optional[str]
    reader: _FormatReader


class ClueWeb22Format(Enum):
    value: _FormatInfo

    HTML = _FormatInfo(
        id="html",
        extension=".warc.gz",
        offset_extension=".warc.offset",
        compression=ClueWeb22Compression.GZIP,
        compression_extension=None,
        reader=_read_html,
    )
    INLINK = _FormatInfo(
        id="inlink",
        extension=".json.gz",
        offset_extension=".offset",
        compression=ClueWeb22Compression.GZIP,
        compression_extension=None,
        reader=_read_inlink,
    )
    OUTLINK = _FormatInfo(
        id="outlink",
        extension=".json.gz",
        offset_extension=".offset",
        compression=ClueWeb22Compression.GZIP,
        compression_extension=None,
        reader=_read_outlink,
    )
    TXT = _FormatInfo(
        id="txt",
        extension=".json.gz",
        offset_extension=".offset",
        compression=ClueWeb22Compression.GZIP,
        compression_extension=None,
        reader=_read_txt,
    )
    JPG = _FormatInfo(
        id="jpg",
        extension=NotImplemented,
        offset_extension=NotImplemented,
        compression=NotImplemented,
        compression_extension=NotImplemented,
        reader=_read_jpg,
    )
    VDOM = _FormatInfo(
        id="vdom",
        extension=".zip",
        offset_extension=None,
        compression=ClueWeb22Compression.ZIP,
        compression_extension=".bin",
        reader=_read_vdom,
    )

    @property
    def id(self) -> str:
        """
        ClueWeb22 format as described
        at https://lemurproject.org/clueweb22/docspecs.php#Organization
        """
        return self.value.id

    @property
    def extension(self) -> str:
        """
        File extension of a single compressed file.
        """
        return self.value.extension

    @property
    def offset_extension(self) -> Optional[str]:
        """
        Offset file extension.
        """
        return self.value.offset_extension

    @property
    def compression(self) -> ClueWeb22Compression:
        """
        Compression form as described
        at https://lemurproject.org/clueweb22/docspecs.php#Compression
        """
        return self.value.compression

    @property
    def compression_extension(self) -> Optional[str]:
        """
        File extension of files within the compressed archive.
        """
        return self.value.compression_extension

    @property
    def reader(self) -> _FormatReader:
        """
        Function for reading records from the decompressed files.
        """
        return self.value.reader


class _LanguageInfo(NamedTuple):
    id: str
    tag: str


class ClueWeb22Language(Enum):
    value: _LanguageInfo

    DE = _LanguageInfo(id="de", tag="de")
    EN = _LanguageInfo(id="en", tag="en")
    ES = _LanguageInfo(id="es", tag="es")
    FR = _LanguageInfo(id="fr", tag="fr")
    IT = _LanguageInfo(id="it", tag="it")
    JA = _LanguageInfo(id="ja", tag="ja")
    NL = _LanguageInfo(id="nl", tag="nl")
    PO = _LanguageInfo(id="po", tag="po")
    PT = _LanguageInfo(id="pt", tag="pt")
    ZH = _LanguageInfo(id="zh_chs", tag="zh")
    OTHER = _LanguageInfo(id="other", tag="other-languages")

    @property
    def id(self) -> str:
        """
        ClueWeb22 language ID as described
        at https://lemurproject.org/clueweb22/docspecs.php#Organization
        """
        return self.value.id

    @property
    def tag(self) -> str:
        """
        Shorthand tag to be used as suffix in the dataset ID.
        """
        return self.value.tag


_Combiner = Callable[[Sequence[Iterator[_Record]]], Iterator[_Doc]]
"""
Function for combining iterables of the different format records
to documents. The record iterators are passed to the function
in the same order as specified in the ``ClueWeb22Subset.formats`` field.
"""


class _SubsetInfo(NamedTuple):
    id: str
    tag: str
    formats: Sequence[ClueWeb22Format]
    doc_type: Type[_Doc]
    combiner: _Combiner
    extends: Optional[str]
    hide: bool


class ClueWeb22Subset(Enum):
    value: _SubsetInfo

    L = _SubsetInfo(
        id="L",
        tag="l",
        formats=[ClueWeb22Format.TXT],
        doc_type=ClueWeb22LDoc,
        combiner=_combine_l_docs,
        extends=None,
        hide=True,
    )
    A = _SubsetInfo(
        id="A",
        tag="a",
        formats=[
            ClueWeb22Format.TXT,
            ClueWeb22Format.HTML,
            ClueWeb22Format.INLINK,
            ClueWeb22Format.OUTLINK,
            ClueWeb22Format.VDOM
        ],
        doc_type=ClueWeb22ADoc,
        combiner=_combine_a_docs,
        extends="L",
        hide=True,
    )
    B = _SubsetInfo(
        id="B",
        tag="b",
        formats=[
            ClueWeb22Format.TXT,
            ClueWeb22Format.HTML,
            ClueWeb22Format.INLINK,
            ClueWeb22Format.OUTLINK,
            ClueWeb22Format.VDOM,
            # TODO Enable screenshot once JPGs are released.
            # ClueWeb22Format.JPG,
        ],
        doc_type=ClueWeb22BDoc,
        combiner=_combine_b_docs,
        extends="A",
        hide=False,
    )

    @property
    def id(self) -> str:
        """
        ClueWeb22 subset name as described
        at https://lemurproject.org/clueweb22/index.php#Specs
        """
        return self.value.id

    @property
    def tag(self) -> str:
        """
        Shorthand to be used as suffix in the dataset ID.
        """
        return self.value.tag

    @property
    def formats(self) -> Sequence[ClueWeb22Format]:
        """
        Required formats for constructing a document for this subset.
        """
        return self.value.formats

    @property
    def doc_type(self) -> Type[_Doc]:
        """
        Type of one single document.
        """
        return self.value.doc_type

    @property
    def combiner(self) -> _Combiner:
        """
        Function for combining iterables of the different format records
        to documents. The record iterators are passed to the function
        in the same order as specified in the ``formats`` field.
        """
        return self.value.combiner

    @property
    def extends(self) -> Optional[str]:
        """
        Subset ID that this subset extends, meaning that it supports
        all fields from the extended subset.
        """
        return self.value.extends

    @property
    def hide(self) -> bool:
        """
        Temporary flag to hide subsets that are not yet
        fully implemented or tested.
        """
        return self.value.hide

    @property
    def subset_views(self) -> AbstractSet["ClueWeb22Subset"]:
        """
        All subsets that are supported by this subset.
        This subset can be viewed as any of the subsets it extends
        because it contains all record types from the extended subsets.
        """
        if self.extends is not None:
            extends = next(
                s for s in ClueWeb22Subset
                if s.id == self.extends
            )
            return {self} | extends.subset_views
        return {self}

    @property
    def diff_formats(self) -> AbstractSet[ClueWeb22Format]:
        """
        Find the formats that are included in this subset
        but not in its subset views.
        """
        formats = set(self.formats)
        for subset_view in self.subset_views - {self}:
            formats -= set(subset_view.formats)
        return formats


# Utility classes


class ClueWeb22DocId(NamedTuple):
    """
    ClueWeb22 document ID as described
    at https://lemurproject.org/clueweb22/docspecs.php#DocIds.

    This class can be used to check the ID format and decompose it
    into its individual components, e.g., to construct a file path.
    """

    language: str
    stream: int
    subdirectory: int
    file: int
    doc: int

    @classmethod
    def from_string(cls, doc_id: str) -> "ClueWeb22DocId":
        parts = doc_id.split("-")
        if len(parts) != 4:
            raise ValueError(f"Invalid ClueWeb22 ID: {doc_id}")
        dataset, subdirectory, file, doc_index = parts
        if dataset != "clueweb22":
            raise ValueError(f"Invalid ClueWeb22 ID: {doc_id}")

        language = subdirectory[:-4]
        if not any(
                language == supported_language.id
                for supported_language in ClueWeb22Language
        ):
            raise ValueError(f"Invalid ClueWeb22 ID: {doc_id}")

        stream_id = int(subdirectory[-4:-2])

        subdirectory_id = int(subdirectory[-2:])
        if subdirectory_id > _MAX_SUBDIRECTORIES_PER_STREAM:
            raise ValueError(f"Invalid ClueWeb22 ID: {doc_id}")

        file_id = int(file)
        if file_id > _MAX_FILES_PER_SUBDIRECTORY:
            raise ValueError(f"Invalid ClueWeb22 ID: {doc_id}")

        doc = int(doc_index)

        return ClueWeb22DocId(
            language=language,
            stream=stream_id,
            subdirectory=subdirectory_id,
            file=file_id,
            doc=doc,
        )

    @property
    def path(self) -> str:
        language_path = self.language
        stream_path = f"{language_path}{self.stream:0>2}"
        subdirectory_path = f"{stream_path}{self.subdirectory:0>2}"
        file_path = f"{subdirectory_path}-{self.file:0>2}"
        return join(
            language_path,
            stream_path,
            subdirectory_path,
            file_path
        )

    def __str__(self) -> str:
        return "-".join([
            "clueweb22",
            f"{self.language}{self.stream:0>2}{self.subdirectory:0>2}",
            f"{self.file:0>2}",
            f"{self.doc:0>5}",
        ])


class ClueWeb22FileId(NamedTuple):
    """
    ClueWeb22 file ID as described
    at https://lemurproject.org/clueweb22/docspecs.php#DocIds
    except for the "doc sequence".

    This class can be used to iterate record counts and to align files
    of different types.
    """

    language: str
    stream: int
    subdirectory: int
    file: int

    @classmethod
    def from_path(cls, path: Path) -> "ClueWeb22FileId":
        language = path.parts[-4]
        stream = int(path.parts[-3][-2:])
        subdirectory = int(path.parts[-2][-2:])
        file = int(path.name.split(".")[0].split("-")[1])
        return ClueWeb22FileId(
            language=language,
            stream=stream,
            subdirectory=subdirectory,
            file=file,
        )

    @property
    def path(self) -> str:
        language_path = self.language
        stream_path = f"{language_path}{self.stream:0>2}"
        subdirectory_path = f"{stream_path}{self.subdirectory:0>2}"
        file_path = f"{subdirectory_path}-{self.file:0>2}"
        return join(
            language_path,
            stream_path,
            subdirectory_path,
            file_path
        )

    def __str__(self) -> str:
        return "-".join([
            "clueweb22",
            f"{self.language}{self.stream:0>2}{self.subdirectory:0>2}",
            f"{self.file:0>2}",
        ])


class ClueWeb22Version(NamedTuple):
    """
    ClueWeb22 disk version.
    """

    subset: ClueWeb22Subset
    major: int
    minor: int


# Docs dataset class.


class ClueWeb22Docs(BaseDocs):
    _name: str
    _source: Download
    _subset: ClueWeb22Subset
    _subset_view: ClueWeb22Subset
    _language: Optional[ClueWeb22Language]

    _path_cached: Path | None = None
    _version_cached: ClueWeb22Version | None = None

    def __init__(
            self,
            name: str,
            source: Download,
            subset: ClueWeb22Subset,
            subset_view: ClueWeb22Subset,
            language: Optional[ClueWeb22Language] = None,
    ):
        super().__init__()
        self._name = name
        self._source = source
        self._subset = subset
        self._subset_view = subset_view
        self._language = language
        assert self._subset_view in subset.subset_views  # nosec: B101

    @property
    def subset_view(self) -> ClueWeb22Subset:
        return self._subset_view

    @property
    def language(self) -> Optional[ClueWeb22Language]:
        return self._language

    def docs_path(self, force: bool = True) -> Union[str, PathLike]:
        return self._source.path(force)

    @property
    def path(self) -> Path:
        if self._path_cached is None:
            self._path_cached = Path(self.docs_path())
            assert self.version.major > 0  # nosec: B101
        return self._path_cached

    @property
    def readme(self) -> str:
        readme_path = self.path / "README.txt"
        with readme_path.open("rt", encoding=_ENCODING) as file:
            return file.read()

    @property
    def version(self) -> ClueWeb22Version:
        if self._version_cached is None:
            version_files = list(self.path.glob("version_*"))
            assert len(version_files) == 1  # nosec: B101
            version_file = version_files[0]
            version_file_name = version_file.name
            _, subset_id, version = version_file_name.split("_")
            assert len(subset_id) == 1  # nosec: B101
            subset = next(
                subset
                for subset in ClueWeb22Subset
                if subset.id == subset_id
            )
            major, minor = version.split(".")
            self._version_cached = ClueWeb22Version(subset, int(major),
                                                    int(minor))
        return self._version_cached

    def _record_counts(
            self,
            format_type: ClueWeb22Format,
    ) -> Iterator[Tuple[ClueWeb22FileId, int]]:
        """
        Iterator with the number of documents per file
        for the specified format, in ascending order by file ID.
        """

        counts_dir = self.path / "record_counts"
        format_counts_dir = counts_dir / format_type.id
        language_prefix: str
        if self._language is not None:
            language_prefix = self._language.id
        else:
            language_prefix = ""
        format_counts_files = sorted(format_counts_dir.glob(
            f"{language_prefix}*_counts.csv"
        ))
        for format_counts_file in format_counts_files:
            assert format_counts_file.name.endswith(
                "_counts.csv")  # nosec: B101
            tag = format_counts_file.name[:-11]  # Remove "_counts.csv"
            language = tag[:-2]
            stream = int(tag[-2:])
            with open(format_counts_file, "rt", encoding=_ENCODING) as file:
                csv_reader = reader(file)
                for file_name, count in csv_reader:
                    subdirectory_tag, file_tag = file_name.split("-")
                    file_no = int(file_tag)
                    subdirectory = int(subdirectory_tag[-2:])
                    file_id = ClueWeb22FileId(
                        language,
                        stream,
                        subdirectory,
                        file_no,
                    )
                    yield file_id, int(count)

    def diff_format_record_counts(
            self
    ) -> Iterator[Tuple[ClueWeb22FileId, int]]:
        """
        Iterator with the number of documents per file,
        in ascending order by file ID,
        for one of the diff formats (arbitrarily selected).

        This is useful to find out the actual count of documents
        for a specific subset, even if the base path
        contains a "broader" subset (with possibly more files).
        """
        diff_formats = self._subset.diff_formats
        try:
            diff_format_type = next(iter(diff_formats))
        except StopIteration:
            # FIXME Temporary fix until parsers for ClueWeb22Format.JPG
            #  are implemented for the B subset. Then the JPG format files
            #  can be used to get the record counts.
            return self._record_counts(ClueWeb22Format.HTML)
        return self._record_counts(diff_format_type)

    def record_counts(
            self,
            format_type: ClueWeb22Format,
    ) -> Iterator[Tuple[ClueWeb22FileId, int]]:
        """
        Iterator with the number of documents per file,
        constrained to the selected subset, even if the base path
        contains a "broader" subset (with possibly more files),
        in ascending order by file ID.
        """
        counts = self._record_counts(format_type)
        for diff_file_id, diff_count in self.diff_format_record_counts():
            for file_id, count in counts:
                if file_id == diff_file_id:
                    yield file_id, diff_count
                    break

    def docs_iter(self) -> Iterator[_Doc]:
        return _ClueWeb22Iterator(self)

    def docs_store(self) -> "ClueWeb22Docstore":
        return ClueWeb22Docstore(self)

    def docs_cls(self) -> Type[_Doc]:
        return self._subset_view.doc_type  # type: ignore

    def docs_count(self) -> int:
        return sum(count for _, count in self.diff_format_record_counts())

    def docs_namespace(self) -> str:
        names = [self._name, self._subset.tag]
        if self._subset_view != self._subset:
            names.append(f"as-{self._subset_view.tag}")
        if self._language is not None:
            names.append(self._language.tag)
        return "/".join(names)

    def docs_lang(self) -> Optional[str]:
        if self._language is None:
            return None
        return self._language.tag


# Iterators and doc store classes for accessing multiple documents efficiently.


_FileIterator = Callable[
    [ClueWeb22Format],
    ContextManager[Iterator[IO[bytes]]]
]
"""
Function for iterating over all files for the specified format type.
The iterator should only open the files that actually 
need to be parsed in order to yield the documents 
specified in ``ClueWeb22Docstore.get_many`` or 
by "fancy" iterator slicing in ``ClueWeb22Iterator``.
"""


class _ClueWeb22Iterable(Iterable[_Doc]):
    _subset_view: ClueWeb22Subset
    _file_iterator: _FileIterator

    def __init__(
            self,
            subset_view: ClueWeb22Subset,
            file_iterator: _FileIterator,
    ):
        self._subset_view = subset_view
        self._file_iterator = file_iterator

    def __iter__(self) -> Iterator[_Doc]:
        formats = self._subset_view.formats

        def read_records(
                format_type: ClueWeb22Format,
                files: Iterator[IO[bytes]],
        ) -> Iterator[_Record]:
            for file in files:
                yield from format_type.reader(file)

        with ExitStack() as stack:
            format_files: Iterator[Iterator[IO[bytes]]] = (
                stack.enter_context(self._file_iterator(format_type))
                for format_type in formats
            )
            format_records: Sequence[Iterator[_Record]] = tuple(
                read_records(format_type, files)
                for format_type, files in zip(formats, format_files)
            )
            documents = self._subset_view.combiner(format_records)
            yield from documents


def _fix_missing_line_break(lines: Iterator[str]) -> Iterator[str]:
    """
    Bug fix for offset file `html/ja/ja00/ja0009/ja0009-57.warc.offset`:
    The offset file's last line is missing a line break,
    so we have to split the last line manually like this:
    04723330100472349245\n -> 0472333010\n0472349245\n
    """
    for line in lines:
        if len(line) > 10 + 1:  # 10 digits and newline
            yield line[:10]
            yield line[10:]
        else:
            yield line


def _read_offsets(
        file_path: Path,
        format_type: ClueWeb22Format,
) -> Iterator[int]:
    assert file_path.name.endswith(format_type.extension)  # nosec: B101
    offsets_name = (
        file_path.name[:-len(format_type.extension)] +
        format_type.offset_extension if format_type.offset_extension else ""
    )
    offsets_file_path = file_path.with_name(offsets_name)
    offsets_file: Iterator[str]
    with offsets_file_path.open("rt", encoding=_ENCODING) as offsets_file:
        # Bug fix for offset file `html/ja/ja00/ja0009/ja0009-57.warc.offset`.
        if offsets_name == "ja0009-57.warc.offset":
            offsets_file = _fix_missing_line_break(offsets_file)

        for offset in offsets_file:
            yield int(offset)


def _fix_path(path: str, format_type: ClueWeb22Format) -> str:
    """
    Fix faulty file paths for Chinese outlink files.
    For example, the document "clueweb22-zh_chs0000-00-00000" has
    the following paths for outlink and HTML files:
    /outlink/zh_chs/zh00/zh_chs0000/zh_chs0000-00.json.gz
    /html/zh_chs/zh_chs00/zh_chs0000/zh_chs0000-00.warc.gz
    """
    if format_type == ClueWeb22Format.OUTLINK and "_" in path:
        path = path.replace("zh_chs/zh_chs", "zh_chs/zh")
    return path


class _ClueWeb22Iterator(Iterator[_Doc]):
    _docs: ClueWeb22Docs
    _full_iterator: Iterator[_Doc]

    def __init__(self, docs: ClueWeb22Docs):
        self._docs = docs
        self._full_iterator = iter(
            _ClueWeb22Iterable(self._docs.subset_view,
                               self._file_iterator_all)
        )

    def __next__(self) -> _Doc:
        return next(self._full_iterator)

    @staticmethod
    def _in_slice(
            index: int,
            start: int,
            stop: int,
            step: int,
    ) -> bool:
        if not (start <= index < stop):
            return False
        return (index - start) % step == 0

    def _file_paths(
            self,
            format_type: ClueWeb22Format,
    ) -> Iterator[Tuple[Path, int]]:
        """
        List all available file paths with the number of records within,
        in ascending order by file path.
        """

        format_path = self._docs.path / format_type.id
        suffix = format_type.extension
        for file_id, count in self._docs.record_counts(format_type):
            path = _fix_path(file_id.path, format_type)
            yield format_path / f"{path}{suffix}", count

    @contextmanager
    def _file_iterator_all(
            self,
            format_type: ClueWeb22Format,
    ) -> Iterator[Iterator[IO[bytes]]]:

        def generator() -> Iterator[IO[bytes]]:
            compression = format_type.compression
            compression_extension = format_type.compression_extension
            file_paths = self._file_paths(format_type)
            for file_path, _ in file_paths:
                with file_path.open("rb") as file:
                    if compression == ClueWeb22Compression.GZIP:
                        assert compression_extension is None  # nosec: B101

                        # Read offsets:
                        first_offset = next(_read_offsets(
                            file_path, format_type
                        ))
                        file.seek(first_offset)

                        gzip_file: IO[bytes]
                        with gzip_open(file, "rb") as gzip_file:  # type: ignore
                            yield gzip_file
                    elif compression == ClueWeb22Compression.ZIP:
                        assert compression_extension is not None  # nosec: B101
                        with ZipFile(file, "r") as zip_file:
                            for name in zip_file.namelist():
                                if name.endswith(compression_extension):
                                    yield zip_file.open(name, "r")
                    else:
                        raise ValueError(
                            f"Unknown compression format: {compression}"
                        )

        yield generator()

    @contextmanager
    def _file_iterator_slice(
            self,
            format_type: ClueWeb22Format,
            start: int,
            stop: int,
            step: int,
    ) -> Iterator[Iterator[IO[bytes]]]:
        compression = format_type.compression
        compression_extension = format_type.compression_extension

        def in_slice(index: int) -> bool:
            if not (start <= index < stop):
                return False
            return (index - start) % step == 0

        def overlaps_slice(
                index_start: int,
                index_end: int
        ) -> bool:
            return max(index_start, start) < min(index_end, stop)

        def generator() -> Iterator[IO[bytes]]:
            file_paths = self._file_paths(format_type)
            index_offset = 0
            for file_path, count in file_paths:

                if not overlaps_slice(index_offset, index_offset + count):
                    index_offset += count
                    continue
                file: IO[bytes]
                with file_path.open("rb") as file:
                    if compression == ClueWeb22Compression.GZIP:
                        assert compression_extension is None  # nosec: B101

                        # Map global indices to file indices.
                        file_indices: Collection[int] = range(count)
                        index_file_indices = (
                            (index_offset + file_index, file_index)
                            for file_index in file_indices
                        )

                        # Determine local indices to read.
                        file_indices = {
                            file_index
                            for index, file_index in index_file_indices
                            if in_slice(index)
                        }
                        if len(file_indices) == 0:
                            # No indices in this file.
                            continue

                        # Wrap file to skip unneeded offsets.
                        offsets = _read_offsets(file_path, format_type)
                        file = OffsetIOWrapper.from_offsets(
                            file, offsets, file_indices
                        )

                        # Decompress the wrapped file.
                        gzip_file: IO[bytes]
                        with gzip_open(file, "rb") as gzip_file:  # type: ignore
                            yield gzip_file

                        index_offset += count

                    elif compression == ClueWeb22Compression.ZIP:
                        assert compression_extension is not None  # nosec: B101
                        with ZipFile(file, "r") as zip_file:
                            # Map global indices to ZIP names.
                            index_names = (
                                (index_offset + index, name)
                                for index, name in
                                enumerate(zip_file.namelist())
                            )

                            # Determine ZIP names to read.
                            names = (
                                name
                                for index, name in index_names
                                if in_slice(index)
                            )

                            for name in names:
                                yield zip_file.open(name, "r")

                            index_offset += count
                    else:
                        raise ValueError(
                            f"Unknown compression format: {compression}"
                        )

        yield generator()

    def __getitem__(self, key: Union[int, slice]) -> Union[
        _Doc, Iterator[_Doc]
    ]:
        docs_count = self._docs.docs_count()
        full_slice = slice(0, docs_count)
        processed_slice: slice
        if isinstance(key, slice):
            processed_slice = apply_sub_slice(full_slice, key)
        elif isinstance(key, int):
            processed_slice = slice_idx(full_slice, key)
        else:
            raise TypeError("key must be int or slice")

        start, stop, step = processed_slice.indices(docs_count)

        def filter_files(
                format_type: ClueWeb22Format,
        ) -> ContextManager[Iterator[IO[bytes]]]:
            return self._file_iterator_slice(
                format_type, start, stop, step
            )

        iterator: Iterator[_Doc] = iter(
            _ClueWeb22Iterable(self._docs.subset_view, filter_files)
        )
        if isinstance(key, slice):
            return iterator

        try:
            return next(iterator)
        except StopIteration:
            raise IndexError(
                (full_slice, slice(key, key + 1), processed_slice))


class ClueWeb22Docstore(Docstore):
    _docs: ClueWeb22Docs

    def __init__(self, docs: ClueWeb22Docs):
        super().__init__(docs.docs_cls(), "doc_id")
        self._docs = docs

    def _file_paths(
            self,
            format_type: ClueWeb22Format,
            doc_ids: Iterable[ClueWeb22DocId],
    ) -> Iterator[Tuple[Path, Iterator[ClueWeb22DocId]]]:
        if self._docs.language is not None:
            invalid_doc_ids = {
                doc_id
                for doc_id in doc_ids
                if doc_id.language != self._docs.language.id
            }
            if len(invalid_doc_ids) > 0:
                raise ValueError(
                    f"The following document IDs don't match "
                    f"the dataset's language ({self._docs.language.id}): "
                    f"{invalid_doc_ids}"
                )

        format_path = self._docs.path / format_type.id

        def doc_id_format_path(doc_id: ClueWeb22DocId) -> Path:
            path = _fix_path(doc_id.path, format_type)
            return format_path / f"{path}{format_type.extension}"

        return (
            (path, path_doc_ids)
            for path, path_doc_ids in groupby(doc_ids, doc_id_format_path)
        )

    @contextmanager
    def _file_iterator(
            self,
            format_type: ClueWeb22Format,
            doc_ids: Iterable[ClueWeb22DocId],
    ) -> Iterator[Iterator[IO[bytes]]]:
        compression = format_type.compression
        compression_extension = format_type.compression_extension

        def generator() -> Iterator[IO[bytes]]:
            file_paths = self._file_paths(format_type, doc_ids)
            for file_path, file_path_doc_ids in file_paths:
                file: IO[bytes]
                with file_path.open("rb") as file:
                    if compression == ClueWeb22Compression.GZIP:
                        assert compression_extension is None  # nosec: B101

                        # Determine indices of documents within the file.
                        indices = {
                            doc_id.doc
                            for doc_id in file_path_doc_ids
                        }

                        # Wrap file to skip unneeded offsets.
                        offsets = _read_offsets(file_path, format_type)
                        file = OffsetIOWrapper.from_offsets(
                            file, offsets, indices
                        )

                        # Decompress the wrapped file.
                        gzip_file: IO[bytes]
                        with gzip_open(file, "rb") as gzip_file:  # type: ignore
                            yield gzip_file

                    elif compression == ClueWeb22Compression.ZIP:
                        assert compression_extension is not None  # nosec: B101
                        with ZipFile(file, "r") as zip_file:
                            names = {
                                f"{doc_id}{compression_extension}"
                                for doc_id in file_path_doc_ids
                            }
                            missing_names = names.difference(
                                zip_file.namelist()
                            )
                            if len(missing_names) > 0:
                                raise RuntimeError(
                                    f"Zip archive at {file_path} is missing "
                                    f"files: {missing_names}"
                                )

                            for name in names:
                                yield zip_file.open(name, "r")
                    else:
                        raise ValueError(
                            f"Unknown compression format: {compression}"
                        )

        yield generator()

    def get_many_iter(self, doc_ids: Iterable[str]) -> Iterator[_Doc]:
        typed_doc_ids: AbstractSet[ClueWeb22DocId] = {
            ClueWeb22DocId.from_string(doc_id)
            for doc_id in doc_ids
        }

        def filter_files(
                format_type: ClueWeb22Format,
        ) -> ContextManager[Iterator[IO[bytes]]]:
            return self._file_iterator(format_type, typed_doc_ids)

        return iter(_ClueWeb22Iterable(self._docs.subset_view, filter_files))


def register() -> None:
    if "clueweb22" in registry:
        # Already registered.
        return

    documentation = YamlDocumentation("clueweb22.yaml")
    base_path = home_path() / "clueweb22"
    download = DownloadConfig.context("clueweb22", base_path)

    registry.register(
        "clueweb22",
        Dataset(documentation("_")),
    )

    for subset in ClueWeb22Subset:
        if subset.hide:
            continue
        subset_tag = subset.tag
        registry.register(
            f"clueweb22/{subset_tag}",
            Dataset(
                documentation(subset_tag),
                ClueWeb22Docs(
                    name="clueweb22",
                    source=download["docs"],
                    subset=subset,
                    subset_view=subset,
                )
            ),
        )
        for language in ClueWeb22Language:
            language_tag = f"{subset_tag}/{language.tag}"
            registry.register(
                f"clueweb22/{language_tag}",
                Dataset(
                    documentation(language_tag),
                    ClueWeb22Docs(
                        name="clueweb22",
                        source=download["docs"],
                        subset=subset,
                        subset_view=subset,
                        language=language,
                    )
                ),
            )
        for subset_view in subset.subset_views - {subset}:
            subset_view_tag = f"{subset_tag}/as-{subset_view.tag}"
            registry.register(
                f"clueweb22/{subset_view_tag}",
                Dataset(
                    documentation(subset_view_tag),
                    ClueWeb22Docs(
                        name="clueweb22",
                        source=download["docs"],
                        subset=subset,
                        subset_view=subset_view,
                    )
                ),
            )
            for language in ClueWeb22Language:
                language_tag = f"{subset_view_tag}/{language.value.tag}"
                registry.register(
                    f"clueweb22/{language_tag}",
                    Dataset(
                        documentation(language_tag),
                        ClueWeb22Docs(
                            name="clueweb22",
                            source=download["docs"],
                            subset=subset,
                            subset_view=subset_view,
                            language=language,
                        )
                    ),
                )
