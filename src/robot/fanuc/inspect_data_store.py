"""
inspect_data_store.py
======================
检查多集数据存储：验证 Parquet、MP4 和 meta 文件。
"""

import json
import os
from pathlib import Path

try:
    import pandas as pd
    import pyarrow.parquet as pq
    HAS_PARQUET = True
except ImportError:
    HAS_PARQUET = False

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


def inspect_data_store(data_root: str = "./data") -> None:
    """
    完整检查数据存储结构和内容。
    """
    data_root = Path(data_root)
    
    if not data_root.exists():
        print(f"❌ Data root not found: {data_root}")
        return
    
    print(f"=" * 70)
    print(f"📂 Inspecting: {data_root}")
    print(f"=" * 70)
    
    # 1. Meta files
    print("\n📋 META FILES:")
    print("-" * 70)
    meta_dir = data_root / "meta"
    
    if (meta_dir / "info.json").exists():
        with open(meta_dir / "info.json") as f:
            info = json.load(f)
        print(f"✅ info.json:")
        print(f"   - Version: {info.get('codebase_version', 'N/A')}")
        print(f"   - FPS: {info.get('fps', 'N/A')}")
        print(f"   - Created: {info.get('created_at', 'N/A')}")
        print(f"   - Features: {', '.join(info.get('schema', {}).get('features', []))}")
    
    if (meta_dir / "stats.json").exists():
        with open(meta_dir / "stats.json") as f:
            stats = json.load(f)
        print(f"\n✅ stats.json ({len(stats)} features):")
        for feat_name, feat_stats in list(stats.items())[:5]:
            print(f"   - {feat_name}: mean={feat_stats.get('mean', 0):.3f}, "
                  f"std={feat_stats.get('std', 0):.3f}")
        if len(stats) > 5:
            print(f"   ... and {len(stats) - 5} more features")
    
    # 2. Parquet data shards
    print(f"\n📊 PARQUET DATA SHARDS:")
    print("-" * 70)
    data_dir = data_root / "data"
    
    if data_dir.exists():
        parquet_files = sorted(data_dir.glob("*.parquet"))
        if parquet_files:
            if HAS_PARQUET:
                total_rows = 0
                for pf in parquet_files:
                    table = pq.read_table(pf)
                    num_rows = len(table)
                    total_rows += num_rows
                    print(f"✅ {pf.name}: {num_rows} rows, {len(table.column_names)} columns")
                
                print(f"\n📈 Total: {total_rows} data rows across {len(parquet_files)} shards")
                
                # 显示列名
                if parquet_files:
                    first_table = pq.read_table(parquet_files[0])
                    print(f"Columns: {', '.join(first_table.column_names)}")
            else:
                print(f"{len(parquet_files)} Parquet files found (install pyarrow to inspect)")
        else:
            print("❌ No Parquet data shards found")
    else:
        print("❌ No data/ directory")
    
    # 3. Episode metadata
    print(f"\n🎬 EPISODE METADATA:")
    print("-" * 70)
    episodes_dir = meta_dir / "episodes"
    
    if episodes_dir.exists():
        episode_files = sorted(episodes_dir.glob("*.parquet"))
        if episode_files:
            if HAS_PARQUET:
                total_episodes = 0
                for ef in episode_files:
                    table = pq.read_table(ef)
                    num_episodes = len(table)
                    total_episodes += num_episodes
                    print(f"✅ {ef.name}: {num_episodes} episodes")
                    
                    # 显示第一个 episode 的信息
                    if num_episodes > 0:
                        first_ep = table.to_pandas().iloc[0]
                        print(f"   First episode: {first_ep.get('episode_id', 'N/A')}")
                        print(f"   - Task: {first_ep.get('task', 'N/A')}")
                        print(f"   - Data offset: {first_ep.get('data_offset_start', 0)}"
                              f" - {first_ep.get('data_offset_end', 0)}")
                        print(f"   - Started: {first_ep.get('timestamp_start', 'N/A')}")
                
                print(f"\n📊 Total: {total_episodes} episodes across {len(episode_files)} shards")
            else:
                print(f"{len(episode_files)} episode metadata files found (install pyarrow to inspect)")
        else:
            print("❌ No episode metadata found")
    else:
        print("❌ No meta/episodes/ directory")
    
    # 4. Video files
    print(f"\n🎥 VIDEO FILES:")
    print("-" * 70)
    videos_dir = data_root / "videos"
    
    if videos_dir.exists():
        video_files = sorted(videos_dir.glob("*.mp4"))
        if video_files:
            for vf in video_files:
                file_size_mb = vf.stat().st_size / (1024 * 1024)
                
                if HAS_CV2:
                    cap = cv2.VideoCapture(str(vf))
                    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                    fps = cap.get(cv2.CAP_PROP_FPS)
                    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    cap.release()
                    
                    print(f"✅ {vf.name}:")
                    print(f"   - Size: {file_size_mb:.2f} MB")
                    print(f"   - Resolution: {width}x{height}")
                    print(f"   - Frames: {frame_count}")
                    print(f"   - FPS: {fps:.1f}")
                else:
                    print(f"✅ {vf.name}: {file_size_mb:.2f} MB (install opencv-python to inspect)")
        else:
            print("❌ No video files found")
    else:
        print("❌ No videos/ directory")
    
    print(f"\n{'=' * 70}")
    print(f"✅ Inspection complete!")


if __name__ == "__main__":
    import sys
    
    data_root = sys.argv[1] if len(sys.argv) > 1 else "./data"
    inspect_data_store(data_root)
