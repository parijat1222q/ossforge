from ossforge import audit, external


def test_builtin_scan_does_not_attribute_external_symlink_source(tmp_path):
    root = tmp_path/'repo'
    root.mkdir()
    outside = tmp_path/'outside.py'
    outside.write_text('def bad(x=[]):\n    return x\n')
    (root/'linked.py').symlink_to(outside)
    (root/'inside.py').write_text('def bad(x=[]):\n    return x\n')
    findings = audit.scan_tree(root)
    assert {f.path for f in findings} == {'inside.py'}


def test_js_scan_does_not_attribute_external_symlink_source(tmp_path):
    root = tmp_path/'repo'
    root.mkdir()
    outside = tmp_path/'outside.js'
    outside.write_text('items.forEach(async (x) => await work(x));\n')
    (root/'linked.js').symlink_to(outside)
    (root/'inside.js').write_text('items.forEach(async (x) => await work(x));\n')
    findings = external.js_findings(root)
    assert {f.path for f in findings} == {'inside.js'}


def test_internal_file_symlinks_and_broken_links_are_not_scanned(tmp_path):
    (tmp_path/'source.py').write_text('def bad(x=[]):\n    return x\n')
    (tmp_path/'alias.py').symlink_to('source.py')
    (tmp_path/'broken.py').symlink_to('missing.py')
    assert {f.path for f in audit.scan_tree(tmp_path)} == {'source.py'}
