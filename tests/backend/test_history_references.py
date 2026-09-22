"""历史裁剪与删除不得破坏画布仍引用的生成产物(已知问题 #1 回归)。"""

from backend.storage import Documents, History


def make_history(history, prompt, data=b"image-bytes"):
    return history.save(f"job-{prompt}", "mock", {"prompt": prompt}, data, "png")


def fill_history_to_limit(history):
    """填满 500 条未收藏记录,使最早一条在下一次保存时被裁剪。"""
    for i in range(500):
        make_history(history, f"p{i}", b"x")


def test_prune_keeps_files_still_referenced_by_canvas(tmp_path):
    """历史裁剪后,画布文档仍引用的生成产物文件不被删除。"""
    documents = Documents(tmp_path / "documents")
    history = History(tmp_path)
    referenced = make_history(history, "被画布引用")
    doc = documents.create("画布")
    documents.save(
        doc["id"],
        {"objects": {"o1": {"src": referenced["url"], "ext": "png"}}, "order": ["o1"]},
    )

    fill_history_to_limit(history)
    make_history(history, "触发裁剪", b"new")

    assert (tmp_path / "images" / referenced["file"]).exists()
    assert referenced["id"] not in [r["id"] for r in history.list(limit=1000)]  # 索引仍按上限收敛


def test_prune_still_deletes_unreferenced_files(tmp_path):
    """历史裁剪后,无画布引用的产物文件被正常清理。"""
    history = History(tmp_path)
    orphan = make_history(history, "无引用")

    fill_history_to_limit(history)
    make_history(history, "触发裁剪", b"new")

    assert not (tmp_path / "images" / orphan["file"]).exists()


def test_delete_history_keeps_file_referenced_by_canvas(tmp_path):
    """删除单条历史时,画布仍引用的产物文件不被删除。"""
    documents = Documents(tmp_path / "documents")
    history = History(tmp_path)
    referenced = make_history(history, "被画布引用")
    doc = documents.create("画布")
    documents.save(
        doc["id"],
        {"objects": {"o1": {"src": referenced["url"]}}, "order": ["o1"]},
    )

    removed = history.remove(referenced["id"])

    assert removed is not None
    assert (tmp_path / "images" / referenced["file"]).exists()


def test_delete_history_removes_unreferenced_file(tmp_path):
    """删除单条无引用历史时,产物文件被一并清理。"""
    history = History(tmp_path)
    orphan = make_history(history, "无引用")

    removed = history.remove(orphan["id"])

    assert removed is not None
    assert not (tmp_path / "images" / orphan["file"]).exists()


def test_unreadable_document_keeps_all_files(tmp_path):
    """文档不可读时引用状态未知,裁剪不得删除任何文件(数据损坏不静默)。"""
    documents = Documents(tmp_path / "documents")
    history = History(tmp_path)
    suspect = make_history(history, "待裁剪")
    doc = documents.create("损坏文档")
    doc_path = tmp_path / "documents" / f"{doc['id']}.json"
    doc_path.write_bytes(b"\xff\xfe not utf-8")  # 模拟损坏文档

    fill_history_to_limit(history)
    make_history(history, "触发裁剪", b"new")

    assert (tmp_path / "images" / suspect["file"]).exists()
    doc_path.unlink(missing_ok=True)
