# Event Payload Reference / 事件数据字段说明

> 修改字段配置请编辑 `midware/event_payload_config.py`，无需改动引擎代码。

---

## 事件一览

| 事件类型 | 触发条件 | 优先级 |
|---|---|:---:|
| `contact` | 单帧损伤增量 ≥ 5 | 5 |
| `position_change` | 玩家名次发生变化 | 5 |
| `off_track` | `\|track_pos\|` > 1.0（驶出赛道边缘） | 5 |
| `lap_complete` | 圈数计数器递增 | 4 |
| `battle` | 前车距离 < 10 m 且车速正常 | 4 |
| `pace_surge` | 6 秒窗口内速度增量 > 22 km/h 且大油门 | 3 |
| `pace_update` | 距上次解说超过 10 秒（定时刷新） | 1 |

---

## 各事件字段详情

### contact — 碰撞

| 字段 | 类型 | 说明 |
|---|---|---|
| `race_pos` | int | 碰撞时玩家名次 |
| `damage_delta` | float | 本次碰撞造成的损伤增量 |
| `total_damage` | float | 车辆累计总损伤值 |
| `collision_direction` | str | 碰撞方向：`front` / `rear` / `left` / `right` |
| `collision_partner` | dict | 推断碰撞对象 `{car_name, race_pos}`，基于对手传感器+排名推断，非精确值 |

**示例**
```json
{
  "event_type": "contact",
  "race_pos": 3,
  "damage_delta": 18.3,
  "total_damage": 423.0,
  "collision_direction": "right",
  "collision_partner": { "car_name": "car5", "race_pos": 4 }
}
```

---

### position_change — 名次变化

| 字段 | 类型 | 说明 |
|---|---|---|
| `direction` | str | 名次变化方向：`up`（上升）/ `down`（下降） |
| `new_pos` | int | 变化后的名次 |
| `lap` | int | 当前圈数 |
| `rankings` | list | 全场排名快照 `[{car_name, race_pos}, ...]` |

**示例**
```json
{
  "event_type": "position_change",
  "direction": "up",
  "new_pos": 2,
  "lap": 3,
  "rankings": [
    { "car_name": "car1", "race_pos": 1 },
    { "car_name": "player", "race_pos": 2 },
    { "car_name": "car3", "race_pos": 3 }
  ]
}
```

---

### off_track — 出界

| 字段 | 类型 | 说明 |
|---|---|---|
| `race_pos` | int | 当前名次 |
| `side` | str | 出界方向：`left` / `right` |
| `track_pos` | float | 赛道横向位置（±1 为边缘，绝对值越大越偏） |
| `damage_delta` | float | 出界造成的损伤增量（若有） |

**示例**
```json
{
  "event_type": "off_track",
  "race_pos": 4,
  "side": "right",
  "track_pos": 1.24,
  "damage_delta": 0.0
}
```

---

### lap_complete — 完圈

| 字段 | 类型 | 说明 |
|---|---|---|
| `completed_lap` | int | 刚完成的圈数编号 |
| `last_lap_time` | float | 该圈用时（秒） |
| `race_pos` | int | 完圈时名次 |
| `fuel_remaining` | float | 剩余油量（升） |
| `rankings` | list | 全场排名快照 `[{car_name, race_pos}, ...]` |

**示例**
```json
{
  "event_type": "lap_complete",
  "completed_lap": 2,
  "last_lap_time": 92.4,
  "race_pos": 1,
  "fuel_remaining": 36.8,
  "rankings": [
    { "car_name": "player", "race_pos": 1 },
    { "car_name": "car2",   "race_pos": 2 }
  ]
}
```

---

### battle — 近身缠斗

| 字段 | 类型 | 说明 |
|---|---|---|
| `race_pos` | int | 当前名次 |
| `lap` | int | 当前圈数 |
| `front_gap` | float | 与前车距离（米） |
| `rankings` | list | 全场排名快照，用于推断缠斗对象 |

**示例**
```json
{
  "event_type": "battle",
  "race_pos": 2,
  "lap": 3,
  "front_gap": 4.7,
  "rankings": [
    { "car_name": "car1",   "race_pos": 1 },
    { "car_name": "player", "race_pos": 2 }
  ]
}
```

---

### pace_surge — 急加速

| 字段 | 类型 | 说明 |
|---|---|---|
| `race_pos` | int | 当前名次 |
| `lap` | int | 当前圈数 |
| `gear` | int | 当前挡位 |
| `front_gap` | float | 与前车距离（米） |
| `rear_gap` | float | 与后车距离（米） |
| `nearest_gap` | float | 与周围最近车辆距离（米） |

**示例**
```json
{
  "event_type": "pace_surge",
  "race_pos": 3,
  "lap": 2,
  "gear": 4,
  "front_gap": 18.2,
  "rear_gap": 34.5,
  "nearest_gap": 18.2
}
```

---

### pace_update — 定时刷新

| 字段 | 类型 | 说明 |
|---|---|---|
| `race_pos` | int | 当前名次 |
| `lap` | int | 当前圈数 |
| `fuel_remaining` | float | 剩余油量（升） |
| `track_pos` | float | 赛道横向位置（0 = 中心线） |
| `rankings` | list | 全场排名快照 |

**示例**
```json
{
  "event_type": "pace_update",
  "race_pos": 4,
  "lap": 2,
  "fuel_remaining": 38.5,
  "track_pos": 0.12,
  "rankings": [
    { "car_name": "car1",   "race_pos": 1 },
    { "car_name": "player", "race_pos": 4 }
  ]
}
```

---

## 所有可用字段速查

| 字段名 | 类型 | 来源 |
|---|---|---|
| `race_pos` | int | 玩家遥测 |
| `lap` | int | 玩家遥测 |
| `gear` | int | 玩家遥测 |
| `track_pos` | float | 玩家遥测 |
| `fuel_remaining` | float | 玩家遥测 |
| `total_damage` | float | 玩家遥测 |
| `last_lap_time` | float | 玩家遥测 |
| `damage_delta` | float | 6 秒窗口计算值 |
| `front_gap` | float | 对手传感器计算值 |
| `rear_gap` | float | 对手传感器计算值 |
| `nearest_gap` | float | 对手传感器计算值 |
| `direction` | str | 事件推断（position_change） |
| `new_pos` | int | 事件推断（position_change） |
| `side` | str | 事件推断（off_track） |
| `completed_lap` | int | 事件推断（lap_complete） |
| `collision_direction` | str | 对手传感器推断（contact） |
| `collision_partner` | dict | 传感器 + 排名交叉推断（contact） |
| `rankings` | list | 全场排名数据 |

---

## 如何修改字段配置

编辑 `midware/event_payload_config.py` 中的 `EVENT_FIELDS` 字典：

```python
"pace_surge": [
    "race_pos",
    "lap",
    "gear",
    "front_gap",   # 删除此行可去掉前车距离
    "rear_gap",
    "nearest_gap",
    "fuel_remaining",  # 新增字段直接加在这里
],
```

修改后重启中间层即可生效，无需改动其他代码。
