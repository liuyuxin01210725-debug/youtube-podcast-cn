"""Offline native-import fixture; creates no translation or audio and grants no release gate."""
from pathlib import Path
import importlib.util, sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from book_pipeline import create_job,package_job
root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('book_fixture',root/'fixtures/book-integration/generate_sample.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True)
source=fixture.make(out/'sample.epub');create_job(source,out/'job',rights=fixture.RIGHTS)
print(package_job(out/'job').resolve())
