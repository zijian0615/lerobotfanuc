from huggingface_hub import HfApi

api = HfApi()

user = "zijian2022"

datasets = api.list_datasets(author=user)

for ds in datasets:
    print(f"Setting {ds.id} to private")
    api.update_repo_visibility(
        repo_id=ds.id,
        repo_type="dataset",
        private=True
    )