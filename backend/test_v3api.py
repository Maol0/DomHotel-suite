"""
测试 v3 API
"""
import requests
import json

BASE_URL = "http://127.0.0.1:8889/api"

def test_api():
    print("=" * 70)
    print("测试 v3 API")
    print("=" * 70)
    
    # 1. 查询房间状态
    print("\n[1] 查询房间 0101 状态")
    r = requests.get(f"{BASE_URL}/query/room-status/0101")
    print(f"  状态码：{r.status_code}")
    if r.status_code == 200:
        data = r.json()["data"]
        print(f"  房间：{data['room']}")
        print(f"  进行中工单：{len(data['open_work_orders'])} 条")
    
    # 2. 查询工单详情
    print("\n[2] 查询工单详情")
    # 先获取一个工单 ID
    r = requests.get(f"{BASE_URL}/events?event_type=work_order")
    if r.status_code == 200:
        events = r.json()["events"]
        if events:
            wo_id = events[0]["data"].get("wo_id")
            print(f"  工单 ID: {wo_id}")
            r = requests.get(f"{BASE_URL}/query/work-order-detail/{wo_id}")
            if r.status_code == 200:
                detail = r.json()["data"]
                print(f"  房间号：{detail.get('room_no')}")
                print(f"  分配人：{detail.get('assignee_name')}")
    
    # 3. 工单状态转换
    print("\n[3] 工单状态转换")
    if events:
        wo_id = events[0]["data"].get("wo_id")
        r = requests.post(
            f"{BASE_URL}/work-orders/{wo_id}/transition",
            json={"new_status": "in_progress", "operator": "test"}
        )
        print(f"  状态码：{r.status_code}")
        if r.status_code == 200:
            result = r.json()
            print(f"  转换：{result.get('transition')}")
    
    # 4. 查询实体列表
    print("\n[4] 查询员工列表")
    r = requests.get(f"{BASE_URL}/entities/staff")
    if r.status_code == 200:
        entities = r.json()["entities"]
        print(f"  员工数量：{len(entities)}")
        for e in entities[:3]:
            print(f"    - {e['data'].get('name')}")
    
    # 5. 高级查询 - 日报
    print("\n[5] 高级查询 - 日报")
    r = requests.post(
        f"{BASE_URL}/query/advanced",
        json={"query_type": "daily_report", "params": {}}
    )
    if r.status_code == 200:
        report = r.json()["data"]
        print(f"  日期：{report['date']}")
        print(f"  总事件：{report['total_events']}")
        print(f"  按类型：{report['by_type']}")

if __name__ == "__main__":
    test_api()
