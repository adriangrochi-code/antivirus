#!/usr/bin/env python3
"""Menú «Nuevo» de Dolphin y del escritorio (clic derecho > Crear nuevo), como en el Explorador de Windows:
documento de Word, hoja de Excel y presentación de PowerPoint (el archivo de texto ya lo trae KDE).

Los archivos de Office son documentos vacíos y válidos escritos a mano (Office Open XML mínimo, sin macros ni nada
externo), así que la plantilla no depende de que LibreOffice esté instalado al construir la ISO.

  plantillas-nuevo.py <raíz>      → <raíz>/usr/share/templates/*.desktop y .source/
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

# Fecha fija dentro de los zip: la ISO sale igual cada vez que se construye
FECHA = (2026, 1, 1, 0, 0, 0)

R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
OFF = "application/vnd.openxmlformats-officedocument"
XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


def rels(*items: tuple[str, str, str]) -> str:
    cuerpo = "".join(f'<Relationship Id="{i}" Type="{t}" Target="{d}"/>' for i, t, d in items)
    return f'{XML}<Relationships xmlns="{PKG}">{cuerpo}</Relationships>'


def tipos(over: dict[str, str]) -> str:
    cuerpo = ('<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="xml" ContentType="application/xml"/>')
    cuerpo += "".join(f'<Override PartName="{p}" ContentType="{c}"/>' for p, c in over.items())
    return f'{XML}<Types xmlns="{CT}">{cuerpo}</Types>'


def raiz(destino: str) -> str:
    return rels(("rId1", f"{R}/officeDocument", destino))


def docx() -> dict[str, str]:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    return {
        "[Content_Types].xml": tipos({"/word/document.xml": f"{OFF}.wordprocessingml.document.main+xml"}),
        "_rels/.rels": raiz("word/document.xml"),
        # Una página A4 con un párrafo vacío en español
        "word/document.xml": f'{XML}<w:document xmlns:w="{w}"><w:body><w:p><w:pPr><w:rPr><w:lang w:val="es-ES"/>'
                             '</w:rPr></w:pPr></w:p><w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
                             '<w:pgMar w:top="1417" w:right="1701" w:bottom="1417" w:left="1701" w:header="708" '
                             'w:footer="708" w:gutter="0"/></w:sectPr></w:body></w:document>',
    }


def xlsx() -> dict[str, str]:
    s = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    return {
        "[Content_Types].xml": tipos({"/xl/workbook.xml": f"{OFF}.spreadsheetml.sheet.main+xml",
                                      "/xl/worksheets/sheet1.xml": f"{OFF}.spreadsheetml.worksheet+xml"}),
        "_rels/.rels": raiz("xl/workbook.xml"),
        "xl/workbook.xml": f'{XML}<workbook xmlns="{s}" xmlns:r="{R}"><sheets>'
                           '<sheet name="Hoja1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": rels(("rId1", f"{R}/worksheet", "worksheets/sheet1.xml")),
        "xl/worksheets/sheet1.xml": f'{XML}<worksheet xmlns="{s}"><sheetData/></worksheet>',
    }


def pptx() -> dict[str, str]:
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    p = "http://schemas.openxmlformats.org/presentationml/2006/main"
    ns = f'xmlns:a="{a}" xmlns:r="{R}" xmlns:p="{p}"'
    grupo = ('<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
             '<p:grpSpPr/></p:spTree></p:cSld>')
    mapa = ('<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" '
            'accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" '
            'folHlink="folHlink"/>')
    colores = "".join(f'<a:{n}><a:srgbClr val="{v}"/></a:{n}>' for n, v in (
        ("dk1", "000000"), ("lt1", "FFFFFF"), ("dk2", "2E2C35"), ("lt2", "E6E6EA"), ("accent1", "A55BE8"),
        ("accent2", "60A5FA"), ("accent3", "4ADE80"), ("accent4", "FBBF24"), ("accent5", "FB7185"),
        ("accent6", "2DD4BF"), ("hlink", "2563EB"), ("folHlink", "8B3FE0")))
    relleno = '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    linea = f'<a:ln w="9525">{relleno}</a:ln>'
    tema = (f'{XML}<a:theme xmlns:a="{a}" name="TISMA"><a:themeElements>'
            f'<a:clrScheme name="TISMA">{colores}</a:clrScheme>'
            '<a:fontScheme name="TISMA"><a:majorFont><a:latin typeface="Carlito"/><a:ea typeface=""/>'
            '<a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Carlito"/><a:ea typeface=""/>'
            '<a:cs typeface=""/></a:minorFont></a:fontScheme>'
            f'<a:fmtScheme name="TISMA"><a:fillStyleLst>{relleno * 3}</a:fillStyleLst>'
            f'<a:lnStyleLst>{linea * 3}</a:lnStyleLst>'
            f'<a:effectStyleLst>{"<a:effectStyle><a:effectLst/></a:effectStyle>" * 3}</a:effectStyleLst>'
            f'<a:bgFillStyleLst>{relleno * 3}</a:bgFillStyleLst></a:fmtScheme>'
            '</a:themeElements></a:theme>')
    return {
        "[Content_Types].xml": tipos({
            "/ppt/presentation.xml": f"{OFF}.presentationml.presentation.main+xml",
            "/ppt/slideMasters/slideMaster1.xml": f"{OFF}.presentationml.slideMaster+xml",
            "/ppt/slideLayouts/slideLayout1.xml": f"{OFF}.presentationml.slideLayout+xml",
            "/ppt/slides/slide1.xml": f"{OFF}.presentationml.slide+xml",
            "/ppt/theme/theme1.xml": f"{OFF}.theme+xml"}),
        "_rels/.rels": raiz("ppt/presentation.xml"),
        # 16:9, una diapositiva en blanco (como «Nueva presentación» de PowerPoint)
        "ppt/presentation.xml": f'{XML}<p:presentation {ns}>'
                                '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
                                '<p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>'
                                '<p:sldSz cx="12192000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/>'
                                '</p:presentation>',
        "ppt/_rels/presentation.xml.rels": rels(("rId1", f"{R}/slideMaster", "slideMasters/slideMaster1.xml"),
                                                ("rId2", f"{R}/slide", "slides/slide1.xml"),
                                                ("rId3", f"{R}/theme", "theme/theme1.xml")),
        "ppt/slideMasters/slideMaster1.xml": f'{XML}<p:sldMaster {ns}>{grupo}{mapa}<p:sldLayoutIdLst>'
                                             '<p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
                                             '</p:sldMaster>',
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": rels(
            ("rId1", f"{R}/slideLayout", "../slideLayouts/slideLayout1.xml"),
            ("rId2", f"{R}/theme", "../theme/theme1.xml")),
        "ppt/slideLayouts/slideLayout1.xml": f'{XML}<p:sldLayout {ns} type="blank">{grupo}'
                                             '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>',
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": rels(
            ("rId1", f"{R}/slideMaster", "../slideMasters/slideMaster1.xml")),
        "ppt/slides/slide1.xml": f'{XML}<p:sld {ns}>{grupo}<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>',
        "ppt/slides/_rels/slide1.xml.rels": rels(("rId1", f"{R}/slideLayout", "../slideLayouts/slideLayout1.xml")),
        "ppt/theme/theme1.xml": tema,
    }


def escribir_zip(ruta: Path, partes: dict[str, str]) -> None:
    with zipfile.ZipFile(ruta, "w", zipfile.ZIP_DEFLATED) as z:
        for nombre, texto in partes.items():  # [Content_Types].xml primero, como Office
            info = zipfile.ZipInfo(nombre, FECHA)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, texto.encode("utf-8"))


# (archivo de la plantilla, nombre en el menú, pregunta al crearlo, icono, contenido)
PLANTILLAS = [
    ("Documento de texto.docx", "Documento de texto (.docx)...", "Nombre del documento:",
     "application-vnd.openxmlformats-officedocument.wordprocessingml.document", docx),
    ("Hoja de cálculo.xlsx", "Hoja de cálculo (.xlsx)...", "Nombre de la hoja de cálculo:",
     "application-vnd.openxmlformats-officedocument.spreadsheetml.sheet", xlsx),
    ("Presentación.pptx", "Presentación (.pptx)...", "Nombre de la presentación:",
     "application-vnd.openxmlformats-officedocument.presentationml.presentation", pptx),
]


def escribir(raiz_sistema: Path) -> list[Path]:
    base = raiz_sistema / "usr/share/templates"
    fuente = base / ".source"
    fuente.mkdir(parents=True, exist_ok=True)
    hechos = []
    for i, (archivo, nombre, pregunta, icono, partes) in enumerate(PLANTILLAS):
        destino = fuente / archivo
        escribir_zip(destino, partes())
        desktop = base / f"tisma-{i}-{destino.suffix[1:]}.desktop"
        # KNewFileMenu: «Name» en el menú; con «...» (tres puntos, no «…») pide el nombre y propone el mismo sin los
        # puntos. «Comment» es la pregunta y URL la plantilla.
        desktop.write_text("[Desktop Entry]\nVersion=1.0\nType=Link\n"
                           f"Name={nombre}\nComment={pregunta}\nURL=.source/{archivo}\nIcon={icono}\n",
                           encoding="utf-8")
        hechos += [destino, desktop]
    return hechos


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    for p in escribir(Path(sys.argv[1])):
        print(p)
