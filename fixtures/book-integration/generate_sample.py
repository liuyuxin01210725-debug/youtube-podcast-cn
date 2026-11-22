"""Self-authored 2-chapter fixture, no third-party text/images."""
from pathlib import Path
import json,zipfile

CH1='''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>The Garden</title></head><body><h1 id="garden">Chapter 1: The Garden</h1><p id="seeds">On Monday, Mira planted 3 seeds. She did not add fertilizer. She wrote every observation in a notebook.<a epub:type="noteref" href="notes.xhtml#n1">[1]</a></p><table id="counts"><caption>Seeds in two plots</caption><tr><th>Plot</th><th>Seeds</th></tr><tr><td>North</td><td>3</td></tr><tr><td>South</td><td>0</td></tr></table></body></html>'''
CH2='''<html xmlns="http://www.w3.org/1999/xhtml"><head><title>The Notebook</title></head><body><h1 id="notebook">Chapter 2: The Notebook</h1><p id="rain">On Tuesday, rain reached both plots. Mira did not mistake an empty plot for a failed experiment.</p><p id="return">She returned to the notebook from Chapter 1. A clear record preserved what changed, and what did not.</p></body></html>'''
NOTES='''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Notes</title></head><body><aside epub:type="footnote" id="n1"><p>A seed count is not a prediction of how many plants will grow.</p></aside></body></html>'''
NAV='''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head><body><nav epub:type="toc"><ol><li><a href="ch2.xhtml#notebook">Notebook first in navigation, not reading order</a></li><li><a href="ch1.xhtml#garden">Garden</a></li></ol></nav></body></html>'''
OPF='''<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">self-authored-shengjian-books-1</dc:identifier><dc:title>A Garden and a Notebook</dc:title><dc:language>en</dc:language></metadata><manifest><item id="one" href="ch1.xhtml" media-type="application/xhtml+xml"/><item id="two" href="ch2.xhtml" media-type="application/xhtml+xml"/><item id="notes" href="notes.xhtml" media-type="application/xhtml+xml"/><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest><spine><itemref idref="one"/><itemref idref="two"/></spine></package>'''
RIGHTS={'kind':'self-authored','statement':'Entire sample and notes authored for this implementation test; external translation/narration allowed, no private book.','region':'CN','external_processing':True}
def make(path, extra=None):
    files={'mimetype':'application/epub+zip','META-INF/container.xml':'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>','OEBPS/content.opf':OPF,'OEBPS/ch2.xhtml':CH2,'OEBPS/notes.xhtml':NOTES,'OEBPS/ch1.xhtml':CH1,'OEBPS/nav.xhtml':NAV}
    files.update(extra or {})
    with zipfile.ZipFile(path,'w') as z:
        for name,value in files.items():
            info=zipfile.ZipInfo(name,(2000,1,1,0,0,0));z.writestr(info,value)
    return Path(path)
if __name__=='__main__':
    root=Path(__file__).parent;make(root/'garden-notebook.epub');(root/'rights.json').write_text(json.dumps(RIGHTS,indent=2))
    (root/'expected.json').write_text(json.dumps({'chapters':2,'text_blocks':7,'non_spine_note':'OEBPS/notes.xhtml#n1','reading_order':['OEBPS/ch1.xhtml','OEBPS/ch2.xhtml'],'table_values':['North','3','South','0'],'notes_policy':'chapter-end; original href/fragment retained','translation':'not yet generated','audio':'not yet generated'},indent=2))
