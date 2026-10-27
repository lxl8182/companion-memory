"""Minimal, body-free consumer audit records and their journal recovery."""
from .schema import load_versioned, wrap

RECEIPT_EVENT = "service.consumer_receipt"


class ConsumerReceiptStore:
    def __init__(self, *, roots, store, permit):
        self.roots, self.store, self.permit = roots, store, permit

    def load(self, character):
        return load_versioned(self.store, self.roots.character_file(character, "service_receipts.json"),
                              supported=frozenset({1}), default={"receipts": {}})

    def save(self, character, document):
        path = self.roots.character_file(character, "service_receipts.json")
        self.permit.assert_writable("consumer_receipt", str(path))
        self.store.save(path, wrap(document))

    @staticmethod
    def apply(document, payload):
        receipts = document.setdefault("receipts", {})
        receipts[payload["receipt_id"]] = dict(payload)
        while len(receipts) > 2048:
            del receipts[next(iter(receipts))]
        return document

    def replay(self, character, payload):
        self.save(character, self.apply(self.load(character), payload))
