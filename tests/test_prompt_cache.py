import os
import time
from agent_vault.core.storage import VaultStorage

def run_tests():
    db_path = "test_vault.db"
    if os.path.exists(db_path):
        os.remove(db_path)
        
    storage = VaultStorage(db_path)
    
    # Create a dummy dependency file
    with open("dummy_dep.py", "w") as f:
        f.write("print('hello')\n")
        
    # We must cache the file in file_cache first, because search_answer relies on file_cache having the digest
    storage.cache_file("dummy_dep.py", "A dummy file")
        
    prompt = "How does the dummy work?"
    response = "It prints hello."
    deps = ["dummy_dep.py"]
    
    # Cache the answer
    storage.cache_answer(prompt, response, deps)
    print("Answer cached.")
    
    # Search the answer
    res = storage.search_answer(prompt)
    if res == response:
        print("Hit! (Expected)")
    else:
        print("Miss! (Unexpected)")
        
    # Modify the dependency file
    with open("dummy_dep.py", "a") as f:
        f.write("print('world')\n")
        
    # Search the answer again
    res = storage.search_answer(prompt)
    if res is None:
        print("Miss! (Expected, file changed)")
    else:
        print(f"Hit! (Unexpected, returned {res})")
        
    # Clean up
    storage.close()
    if os.path.exists(db_path):
        os.remove(db_path)
    if os.path.exists("dummy_dep.py"):
        os.remove("dummy_dep.py")

if __name__ == "__main__":
    run_tests()
