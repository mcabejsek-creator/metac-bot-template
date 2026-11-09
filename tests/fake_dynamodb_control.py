"""Synthetic DynamoDB subset over the existing one-row SQLite test authority.

NOT DynamoDB Local, an IAM policy simulator or proof of AWS behavior. It
evaluates the emitted condition/SET subset instead of trusting a CAS flag.
All state is in one persisted row. Roles here are test inputs, not identity.
"""

from copy import deepcopy
import json
import re

if __package__:
    from .test_worker_claim_offline import SQLiteControlStore
else:
    from test_worker_claim_offline import SQLiteControlStore


class ModelError(RuntimeError):
    def __init__(self, code):
        super().__init__('Synthetic DynamoDB error')
        self.response = {'Error': {'Code': code}}


class WireItem:
    def __init__(self, item):
        self.item = item

    def encode(self):
        return json.dumps(self.item, sort_keys=True)


class SyntheticDynamoTransport:
    def __init__(self, path, *, principal='broker'):
        self.backend = SQLiteControlStore(path)
        self.principal = principal
        self.calls = []
        self.before_update = self.after_update = self.read_filter = self.response_filter = None

    @classmethod
    def seed(cls, path, item):
        SQLiteControlStore.seed(path, WireItem(item))
        return cls(path)

    def current(self):
        return json.loads(self.backend.read()[0])

    def admin_replace(self, item):
        """Explicit fault setup: privileged writer/restore bypasses the broker."""
        _, revision = self.backend.read()
        if not self.backend.compare_and_swap(revision, WireItem(item).encode()):
            raise ModelError('ConditionalCheckFailedException')

    @staticmethod
    def metadata():
        return {'ResponseMetadata': {'HTTPStatusCode': 200, 'RetryAttempts': 0}}

    def get_item(self, **request):
        self.calls.append(('GetItem', deepcopy(request)))
        if self.principal == 'worker':
            raise ModelError('AccessDeniedException')
        if request.get('ConsistentRead') is not True:
            raise ModelError('ValidationException')
        item = self.current()
        result = {**self.metadata(), **({'Item': item} if item is not None else {})}
        return self.read_filter(result) if self.read_filter else result

    def update_item(self, **request):
        self.calls.append(('UpdateItem', deepcopy(request)))
        if self.principal == 'worker':
            raise ModelError('AccessDeniedException')
        if self.before_update:
            self.before_update(request)
        payload, revision = self.backend.read()
        item = json.loads(payload)
        names = request.get('ExpressionAttributeNames', {})
        values = request.get('ExpressionAttributeValues', {})
        missing = object()

        def path_parts(path):
            return [names.get(name, name) for name in path.split('.')]

        def get(path):
            cursor = item
            parts = path_parts(path)
            for index, part in enumerate(parts):
                if type(cursor) is not dict or part not in cursor:
                    return missing
                value = cursor[part]
                if index == len(parts) - 1:
                    return value
                cursor = value.get('M') if type(value) is dict else None
            return missing

        def condition(clause):
            exists = re.fullmatch(r'attribute_exists\(([^)]+)\)', clause)
            if exists:
                return get(exists[1]) is not missing
            typed = re.fullmatch(r'attribute_type\(([^,]+), (:[^)]+)\)', clause)
            if typed:
                value = get(typed[1])
                return value is not missing and set(value) == {values[typed[2]]['S']}
            comparison = re.fullmatch(r'([^ ]+) (=|<) (:[^ ]+)', clause)
            if comparison:
                actual, op, wanted = get(comparison[1]), comparison[2], values[comparison[3]]
                if actual is missing:
                    return False
                return actual == wanted if op == '=' else int(actual['N']) < int(wanted['N'])
            raise ModelError('ValidationException')

        # DynamoDB's request model permits omitted ConditionExpression. That is
        # why granting raw UpdateItem to an untrusted writer is insufficient.
        expression = request.get('ConditionExpression')
        if expression and not all(condition(clause) for clause in expression.split(' AND ')):
            raise ModelError('ConditionalCheckFailedException')
        if item is None:
            item = {}
        proposed = deepcopy(item)
        update = request.get('UpdateExpression', '')
        if not update.startswith('SET '):
            raise ModelError('ValidationException')
        for assignment in update[4:].split(', '):
            path, rhs = assignment.split(' = ', 1)
            if ' + ' in rhs:
                source, number = rhs.split(' + ')
                value = {'N': str(int(get(source)['N']) + int(values[number]['N']))}
            else:
                value = deepcopy(values[rhs])
            parts = path_parts(path)
            cursor = proposed
            for part in parts[:-1]:
                cursor = cursor[part]['M']
            cursor[parts[-1]] = value
        if not self.backend.compare_and_swap(revision, WireItem(proposed).encode()):
            raise ModelError('ConditionalCheckFailedException')
        result = {**self.metadata(), 'Attributes': deepcopy(proposed)}
        if self.after_update:
            self.after_update(request)
        return self.response_filter(result) if self.response_filter else result
