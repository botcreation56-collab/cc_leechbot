import ast
import os

def get_defined_names(filename):
    with open(filename, 'r', encoding='utf-8') as f:
        tree = ast.parse(f.read(), filename=filename)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names

def check_init_file(init_file, handlers_dir):
    with open(init_file, 'r', encoding='utf-8') as f:
        tree = ast.parse(f.read(), filename=init_file)
    
    missing = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            module = node.module # e.g. 'admin' if from .admin import ...
            if module:
                filepath = os.path.join(handlers_dir, f"{module}.py")
                if os.path.exists(filepath):
                    defined = get_defined_names(filepath)
                    for alias in node.names:
                        if alias.name not in defined:
                            missing.append((module, alias.name))
    return missing

if __name__ == '__main__':
    missing = check_init_file('bot/handlers/__init__.py', 'bot/handlers')
    for m, name in missing:
        print(f"Missing in {m}.py: {name}")
