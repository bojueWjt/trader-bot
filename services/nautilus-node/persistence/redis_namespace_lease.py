from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from threading import Event, RLock, Thread, current_thread
from typing import Any, Protocol
from uuid import UUID, uuid4

DEFAULT_REDIS_NAMESPACE_REGISTRY_KEY = "trader-bot:redis-namespaces:active"
DEFAULT_REDIS_FENCING_EPOCH_KEY = "trader-bot:redis-fencing-epoch"
DEFAULT_REDIS_NAMESPACE_LEASE_MAX_AGE_SECONDS = 120
DEFAULT_REDIS_NAMESPACE_LEASE_REFRESH_INTERVAL_SECONDS = 60.0
DEFAULT_REDIS_NAMESPACE_LEASE_THREAD_JOIN_SECONDS = 5.0
DEFAULT_REDIS_NAMESPACE_LIST_SNAPSHOT_ATTEMPTS = 3
MAX_PERSISTENCE_GENERATION_FENCING_TOKEN = 0xFFFFFFFFFFFF
_ASCII_DECIMAL_PATTERN = re.compile(r"^[0-9]+$")

_ACQUIRE_LUA = r"""
-- redis-namespace-lease:acquire-v7
local registry_key = KEYS[1]
local metadata_key = KEYS[2]
local fencing_key = KEYS[3]
local epoch_key = KEYS[4]
local namespace = ARGV[1]
local owner = ARGV[2]
local release_id = ARGV[3]
local max_age_seconds = tonumber(ARGV[4])
local candidate_instance_id = ARGV[5]
local redis_time = redis.call("TIME")
local refreshed_at_epoch = tonumber(redis_time[1])
local redis_fencing_epoch = redis.call("GET", epoch_key)
local current_json = redis.call("HGET", metadata_key, namespace)
local max_fencing_token = 281474976710655
local max_json_integer_token = 99999999999999
local current_fencing_token = 0

local function owner_matches_node_identity(current_owner)
    if current_owner == owner then
        return true
    end
    local prefix = owner .. ":"
    if string.sub(current_owner, 1, string.len(prefix)) ~= prefix then
        return false
    end
    local suffix = string.sub(
        current_owner,
        string.len(prefix) + 1
    )
    local host_separator = string.find(suffix, ":", 1, true)
    if not host_separator then
        return false
    end
    local host = string.sub(suffix, 1, host_separator - 1)
    local process_and_uuid = string.sub(suffix, host_separator + 1)
    local process_separator = string.find(
        process_and_uuid,
        ":",
        1,
        true
    )
    if not process_separator then
        return false
    end
    local process_id = string.sub(
        process_and_uuid,
        1,
        process_separator - 1
    )
    local uuid = string.sub(
        process_and_uuid,
        process_separator + 1
    )
    return host ~= ""
        and string.match(host, "^[^:]+$") ~= nil
        and string.match(process_id, "^[0-9]+$") ~= nil
        and string.len(uuid) == 32
        and string.match(uuid, "^[0-9a-f]+$") ~= nil
end

local function decode_fencing_token(value)
    local token = nil
    if type(value) == "number" then
        token = value
    elseif type(value) == "string"
        and string.match(value, "^[0-9]+$") then
        token = tonumber(value)
    end
    if not token
        or token < 1
        or token > max_fencing_token
        or token ~= math.floor(token) then
        return nil
    end
    return token
end

local function legacy_persistence_generation(fencing_token)
    local token = decode_fencing_token(fencing_token)
    if not token then
        return nil, nil
    end
    local instance_id = string.format(
        "00000000-0000-4000-8000-%012x",
        token
    )
    return instance_id, namespace .. ":" .. instance_id
end

local function is_canonical_uuid4(instance_id)
    if type(instance_id) ~= "string"
        or string.len(instance_id) ~= 36
        or string.lower(instance_id) ~= instance_id then
        return false
    end
    if string.sub(instance_id, 9, 9) ~= "-"
        or string.sub(instance_id, 14, 14) ~= "-"
        or string.sub(instance_id, 19, 19) ~= "-"
        or string.sub(instance_id, 24, 24) ~= "-"
        or string.sub(instance_id, 15, 15) ~= "4" then
        return false
    end
    local variant = string.sub(instance_id, 20, 20)
    if variant ~= "8"
        and variant ~= "9"
        and variant ~= "a"
        and variant ~= "b" then
        return false
    end
    local compact = string.gsub(instance_id, "-", "")
    return string.len(compact) == 32
        and string.match(compact, "^[0-9a-f]+$") ~= nil
end

local function record_persistence_generation(current, fencing_token)
    local instance_id = current["persistence_instance_id"]
    local persistence_namespace = current["persistence_namespace"]
    local has_instance_id = instance_id ~= nil
    local has_persistence_namespace = persistence_namespace ~= nil
    if not has_instance_id and not has_persistence_namespace then
        return legacy_persistence_generation(fencing_token)
    end
    local namespace_prefix = namespace .. ":"
    if not has_instance_id then
        if type(persistence_namespace) ~= "string"
            or string.sub(
                persistence_namespace,
                1,
                string.len(namespace_prefix)
            ) ~= namespace_prefix then
            return nil, nil
        end
        instance_id = string.sub(
            persistence_namespace,
            string.len(namespace_prefix) + 1
        )
    end
    if not is_canonical_uuid4(instance_id) then
        return nil, nil
    end
    local expected_namespace = namespace_prefix .. instance_id
    if has_persistence_namespace
        and persistence_namespace ~= expected_namespace then
        return nil, nil
    end
    return instance_id, expected_namespace
end

local function persisted_fencing_token(fencing_token)
    if fencing_token > max_json_integer_token then
        return string.format("%.0f", fencing_token)
    end
    return fencing_token
end

if not redis_fencing_epoch then
    return {0, 0, "MISSING_EPOCH"}
end
if not is_canonical_uuid4(redis_fencing_epoch) then
    return {0, 0, "CORRUPT_EPOCH"}
end
if not max_age_seconds
    or max_age_seconds <= 0
    or max_age_seconds ~= math.floor(max_age_seconds) then
    return {0, 0, "INVALID_MAX_AGE"}
end
if not is_canonical_uuid4(candidate_instance_id) then
    return {0, 0, "INVALID_CANDIDATE"}
end
local fresh_after_epoch = refreshed_at_epoch - max_age_seconds

if current_json then
    local decoded, current = pcall(cjson.decode, current_json)
    if not decoded
        or type(current) ~= "table"
        or current["namespace"] ~= namespace
        or type(current["owner"]) ~= "string"
        or current["owner"] == ""
        or type(current["release_id"]) ~= "string"
        or current["release_id"] == "" then
        return {0, 0, "CORRUPT"}
    end
    local fencing_token = decode_fencing_token(current["fencing_token"])
    if not fencing_token then
        return {0, 0, "CORRUPT"}
    end
    local current_redis_fencing_epoch = current["redis_fencing_epoch"]
    if not is_canonical_uuid4(current_redis_fencing_epoch) then
        return {0, fencing_token, "CORRUPT_EPOCH"}
    end
    if current_redis_fencing_epoch ~= redis_fencing_epoch then
        return {0, fencing_token, "EPOCH_MISMATCH"}
    end
    current_fencing_token = fencing_token
    local instance_id, persistence_namespace = record_persistence_generation(
        current,
        fencing_token
    )
    if not instance_id then
        return {0, 0, "CORRUPT"}
    end
    local current_score = redis.call("ZSCORE", registry_key, namespace)
    local current_refreshed_at_epoch = tonumber(
        current["refreshed_at_epoch"]
    )
    if not current_score
        or not current_refreshed_at_epoch
        or tonumber(current_score) ~= current_refreshed_at_epoch then
        return {0, fencing_token, "CORRUPT"}
    end
    local current_is_fresh = (
        current_refreshed_at_epoch > fresh_after_epoch
    )
    if current["owner"] == owner
        and current["release_id"] == release_id
        and candidate_instance_id == instance_id
        and current_is_fresh then
        current["fencing_token"] = persisted_fencing_token(fencing_token)
        current["redis_fencing_epoch"] = redis_fencing_epoch
        current["persistence_instance_id"] = instance_id
        current["persistence_namespace"] = persistence_namespace
        current["refreshed_at_epoch"] = refreshed_at_epoch
        redis.call("HSET", metadata_key, namespace, cjson.encode(current))
        redis.call("ZADD", registry_key, refreshed_at_epoch, namespace)
        return {
            1,
            fencing_token,
            refreshed_at_epoch,
            instance_id,
            persistence_namespace,
            redis_fencing_epoch
        }
    end
    if current_is_fresh then
        return {0, fencing_token, "HELD"}
    end
    if not owner_matches_node_identity(current["owner"]) then
        return {0, fencing_token, "STALE_FOREIGN"}
    end
else
    local legacy_score = redis.call("ZSCORE", registry_key, namespace)
    if legacy_score and tonumber(legacy_score) > fresh_after_epoch then
        return {0, 0, "HELD_LEGACY"}
    end
end

local raw_counter = redis.call("GET", fencing_key)
local counter = 0
if raw_counter then
    if type(raw_counter) ~= "string"
        or not string.match(raw_counter, "^[0-9]+$") then
        return {0, 0, "CORRUPT_COUNTER"}
    end
    counter = tonumber(raw_counter)
    if not counter
        or counter < 0
        or counter > max_fencing_token
        or counter ~= math.floor(counter) then
        return {0, 0, "CORRUPT_COUNTER"}
    end
end
if current_fencing_token > counter then
    counter = current_fencing_token
    redis.call("SET", fencing_key, string.format("%.0f", counter))
end
if counter >= max_fencing_token then
    return {0, 0, "OVERFLOW"}
end

local fencing_token = redis.call("INCR", fencing_key)
if not fencing_token
    or fencing_token < 1
    or fencing_token > max_fencing_token
    or fencing_token ~= math.floor(fencing_token) then
    return {0, 0, "OVERFLOW"}
end
local instance_id = candidate_instance_id
local persistence_namespace = namespace .. ":" .. instance_id
local record = {
    namespace = namespace,
    owner = owner,
    release_id = release_id,
    redis_fencing_epoch = redis_fencing_epoch,
    fencing_token = persisted_fencing_token(fencing_token),
    persistence_instance_id = instance_id,
    persistence_namespace = persistence_namespace,
    refreshed_at_epoch = refreshed_at_epoch
}
redis.call("HSET", metadata_key, namespace, cjson.encode(record))
redis.call("ZADD", registry_key, refreshed_at_epoch, namespace)
return {
    1,
    fencing_token,
    refreshed_at_epoch,
    instance_id,
    persistence_namespace,
    redis_fencing_epoch
}
"""

_REFRESH_LUA = r"""
-- redis-namespace-lease:refresh-v5
local registry_key = KEYS[1]
local metadata_key = KEYS[2]
local epoch_key = KEYS[3]
local namespace = ARGV[1]
local owner = ARGV[2]
local release_id = ARGV[3]
local raw_fencing_token = ARGV[4]
local max_age_seconds = tonumber(ARGV[5])
local expected_redis_fencing_epoch = ARGV[6]
local redis_time = redis.call("TIME")
local refreshed_at_epoch = tonumber(redis_time[1])
local redis_fencing_epoch = redis.call("GET", epoch_key)
local current_json = redis.call("HGET", metadata_key, namespace)
local max_fencing_token = 281474976710655
local max_json_integer_token = 99999999999999

local function decode_fencing_token(value)
    local token = nil
    if type(value) == "number" then
        token = value
    elseif type(value) == "string"
        and string.match(value, "^[0-9]+$") then
        token = tonumber(value)
    end
    if not token
        or token < 1
        or token > max_fencing_token
        or token ~= math.floor(token) then
        return nil
    end
    return token
end

local function legacy_persistence_generation(token)
    local decoded_token = decode_fencing_token(token)
    if not decoded_token then
        return nil, nil
    end
    local instance_id = string.format(
        "00000000-0000-4000-8000-%012x",
        decoded_token
    )
    return instance_id, namespace .. ":" .. instance_id
end

local function is_canonical_uuid4(instance_id)
    if type(instance_id) ~= "string"
        or string.len(instance_id) ~= 36
        or string.lower(instance_id) ~= instance_id then
        return false
    end
    if string.sub(instance_id, 9, 9) ~= "-"
        or string.sub(instance_id, 14, 14) ~= "-"
        or string.sub(instance_id, 19, 19) ~= "-"
        or string.sub(instance_id, 24, 24) ~= "-"
        or string.sub(instance_id, 15, 15) ~= "4" then
        return false
    end
    local variant = string.sub(instance_id, 20, 20)
    if variant ~= "8"
        and variant ~= "9"
        and variant ~= "a"
        and variant ~= "b" then
        return false
    end
    local compact = string.gsub(instance_id, "-", "")
    return string.len(compact) == 32
        and string.match(compact, "^[0-9a-f]+$") ~= nil
end

local function record_persistence_generation(current, token)
    local instance_id = current["persistence_instance_id"]
    local persistence_namespace = current["persistence_namespace"]
    local has_instance_id = instance_id ~= nil
    local has_persistence_namespace = persistence_namespace ~= nil
    if not has_instance_id and not has_persistence_namespace then
        return legacy_persistence_generation(token)
    end
    local namespace_prefix = namespace .. ":"
    if not has_instance_id then
        if type(persistence_namespace) ~= "string"
            or string.sub(
                persistence_namespace,
                1,
                string.len(namespace_prefix)
            ) ~= namespace_prefix then
            return nil, nil
        end
        instance_id = string.sub(
            persistence_namespace,
            string.len(namespace_prefix) + 1
        )
    end
    if not is_canonical_uuid4(instance_id) then
        return nil, nil
    end
    local expected_namespace = namespace_prefix .. instance_id
    if has_persistence_namespace
        and persistence_namespace ~= expected_namespace then
        return nil, nil
    end
    return instance_id, expected_namespace
end

local function persisted_fencing_token(token)
    if token > max_json_integer_token then
        return string.format("%.0f", token)
    end
    return token
end

if not redis_fencing_epoch then
    return {0, "MISSING_EPOCH", refreshed_at_epoch}
end
if not is_canonical_uuid4(redis_fencing_epoch) then
    return {0, "CORRUPT_EPOCH", refreshed_at_epoch}
end
if not is_canonical_uuid4(expected_redis_fencing_epoch) then
    return {0, "INVALID_RECORD_EPOCH", refreshed_at_epoch}
end
if redis_fencing_epoch ~= expected_redis_fencing_epoch then
    return {0, "EPOCH_CHANGED", refreshed_at_epoch}
end
if not max_age_seconds
    or max_age_seconds <= 0
    or max_age_seconds ~= math.floor(max_age_seconds) then
    return {0, "INVALID_MAX_AGE", refreshed_at_epoch}
end
local fencing_token = decode_fencing_token(raw_fencing_token)
if not fencing_token then
    return {0, "INVALID_FENCING_TOKEN", refreshed_at_epoch}
end
if not current_json then
    return {0, "MISSING", refreshed_at_epoch}
end

local decoded, current = pcall(cjson.decode, current_json)
if not decoded or type(current) ~= "table" then
    return {0, "CORRUPT", refreshed_at_epoch}
end
local current_fencing_token = decode_fencing_token(
    current["fencing_token"]
)
if not current_fencing_token then
    return {0, "CORRUPT", refreshed_at_epoch}
end
local current_redis_fencing_epoch = current["redis_fencing_epoch"]
if not is_canonical_uuid4(current_redis_fencing_epoch) then
    return {0, "CORRUPT_EPOCH", refreshed_at_epoch}
end
if current_redis_fencing_epoch ~= redis_fencing_epoch then
    return {0, "EPOCH_MISMATCH", refreshed_at_epoch}
end
local expected_instance_id, expected_namespace = record_persistence_generation(
    current,
    current_fencing_token
)
if not expected_instance_id then
    return {0, "CORRUPT", refreshed_at_epoch}
end
if current["owner"] ~= owner
    or current["release_id"] ~= release_id
    or current["namespace"] ~= namespace
    or current_fencing_token ~= fencing_token then
    return {0, "FENCED", refreshed_at_epoch}
end
local current_score = redis.call("ZSCORE", registry_key, namespace)
local current_refreshed_at_epoch = tonumber(current["refreshed_at_epoch"])
if not current_score
    or not current_refreshed_at_epoch
    or tonumber(current_score) ~= current_refreshed_at_epoch then
    return {0, "CORRUPT", refreshed_at_epoch}
end
local fresh_after_epoch = refreshed_at_epoch - max_age_seconds
if current_refreshed_at_epoch <= fresh_after_epoch then
    return {0, "EXPIRED", refreshed_at_epoch}
end

current["fencing_token"] = persisted_fencing_token(current_fencing_token)
current["redis_fencing_epoch"] = redis_fencing_epoch
current["persistence_instance_id"] = expected_instance_id
current["persistence_namespace"] = expected_namespace
current["refreshed_at_epoch"] = refreshed_at_epoch
redis.call("HSET", metadata_key, namespace, cjson.encode(current))
redis.call("ZADD", registry_key, refreshed_at_epoch, namespace)
return {
    1,
    "REFRESHED",
    refreshed_at_epoch,
    expected_instance_id,
    expected_namespace,
    redis_fencing_epoch
}
"""

_REMOVE_LUA = r"""
-- redis-namespace-lease:remove-v4
local registry_key = KEYS[1]
local metadata_key = KEYS[2]
local epoch_key = KEYS[3]
local namespace = ARGV[1]
local owner = ARGV[2]
local release_id = ARGV[3]
local raw_fencing_token = ARGV[4]
local expected_redis_fencing_epoch = ARGV[5]
local redis_fencing_epoch = redis.call("GET", epoch_key)
local current_json = redis.call("HGET", metadata_key, namespace)
local max_fencing_token = 281474976710655

local function decode_fencing_token(value)
    local token = nil
    if type(value) == "number" then
        token = value
    elseif type(value) == "string"
        and string.match(value, "^[0-9]+$") then
        token = tonumber(value)
    end
    if not token
        or token < 1
        or token > max_fencing_token
        or token ~= math.floor(token) then
        return nil
    end
    return token
end

local function legacy_persistence_generation(token)
    local decoded_token = decode_fencing_token(token)
    if not decoded_token then
        return nil, nil
    end
    local instance_id = string.format(
        "00000000-0000-4000-8000-%012x",
        decoded_token
    )
    return instance_id, namespace .. ":" .. instance_id
end

local function is_canonical_uuid4(instance_id)
    if type(instance_id) ~= "string"
        or string.len(instance_id) ~= 36
        or string.lower(instance_id) ~= instance_id then
        return false
    end
    if string.sub(instance_id, 9, 9) ~= "-"
        or string.sub(instance_id, 14, 14) ~= "-"
        or string.sub(instance_id, 19, 19) ~= "-"
        or string.sub(instance_id, 24, 24) ~= "-"
        or string.sub(instance_id, 15, 15) ~= "4" then
        return false
    end
    local variant = string.sub(instance_id, 20, 20)
    if variant ~= "8"
        and variant ~= "9"
        and variant ~= "a"
        and variant ~= "b" then
        return false
    end
    local compact = string.gsub(instance_id, "-", "")
    return string.len(compact) == 32
        and string.match(compact, "^[0-9a-f]+$") ~= nil
end

local function record_persistence_generation(current, token)
    local instance_id = current["persistence_instance_id"]
    local persistence_namespace = current["persistence_namespace"]
    local has_instance_id = instance_id ~= nil
    local has_persistence_namespace = persistence_namespace ~= nil
    if not has_instance_id and not has_persistence_namespace then
        return legacy_persistence_generation(token)
    end
    local namespace_prefix = namespace .. ":"
    if not has_instance_id then
        if type(persistence_namespace) ~= "string"
            or string.sub(
                persistence_namespace,
                1,
                string.len(namespace_prefix)
            ) ~= namespace_prefix then
            return nil, nil
        end
        instance_id = string.sub(
            persistence_namespace,
            string.len(namespace_prefix) + 1
        )
    end
    if not is_canonical_uuid4(instance_id) then
        return nil, nil
    end
    local expected_namespace = namespace_prefix .. instance_id
    if has_persistence_namespace
        and persistence_namespace ~= expected_namespace then
        return nil, nil
    end
    return instance_id, expected_namespace
end

if not redis_fencing_epoch then
    return {0, "MISSING_EPOCH"}
end
if not is_canonical_uuid4(redis_fencing_epoch) then
    return {0, "CORRUPT_EPOCH"}
end
if not is_canonical_uuid4(expected_redis_fencing_epoch) then
    return {0, "INVALID_RECORD_EPOCH"}
end
if redis_fencing_epoch ~= expected_redis_fencing_epoch then
    return {0, "EPOCH_CHANGED"}
end
if not current_json then
    return {0, "MISSING_IDENTITY"}
end

local fencing_token = decode_fencing_token(raw_fencing_token)
if not fencing_token then
    return {0, "INVALID_FENCING_TOKEN"}
end
local decoded, current = pcall(cjson.decode, current_json)
if not decoded or type(current) ~= "table" then
    return {0, "CORRUPT"}
end
local current_fencing_token = decode_fencing_token(
    current["fencing_token"]
)
if not current_fencing_token then
    return {0, "CORRUPT"}
end
local current_redis_fencing_epoch = current["redis_fencing_epoch"]
if not is_canonical_uuid4(current_redis_fencing_epoch) then
    return {0, "CORRUPT_EPOCH"}
end
if current_redis_fencing_epoch ~= redis_fencing_epoch then
    return {0, "EPOCH_MISMATCH"}
end
local expected_instance_id, expected_namespace = record_persistence_generation(
    current,
    current_fencing_token
)
if not expected_instance_id then
    return {0, "CORRUPT"}
end
if current["owner"] ~= owner
    or current["release_id"] ~= release_id
    or current["namespace"] ~= namespace
    or current_fencing_token ~= fencing_token then
    return {0, "FENCED"}
end

redis.call("HDEL", metadata_key, namespace)
redis.call("ZREM", registry_key, namespace)
return {1, "REMOVED"}
"""

_FORCE_REMOVE_STALE_LUA = r"""
-- redis-namespace-lease:force-remove-stale-v3
local registry_key = KEYS[1]
local metadata_key = KEYS[2]
local epoch_key = KEYS[3]
local namespace = ARGV[1]
local max_age_seconds = tonumber(ARGV[2])
local server_time = tonumber(redis.call("TIME")[1])
local redis_fencing_epoch = redis.call("GET", epoch_key)
local current_json = redis.call("HGET", metadata_key, namespace)
local current_score = redis.call("ZSCORE", registry_key, namespace)
local max_fencing_token = 281474976710655

local function decode_fencing_token(value)
    local token = nil
    if type(value) == "number" then
        token = value
    elseif type(value) == "string"
        and string.match(value, "^[0-9]+$") then
        token = tonumber(value)
    end
    if not token
        or token < 1
        or token > max_fencing_token
        or token ~= math.floor(token) then
        return nil
    end
    return token
end

local function legacy_persistence_generation(fencing_token)
    local token = decode_fencing_token(fencing_token)
    if not token then
        return nil, nil
    end
    local instance_id = string.format(
        "00000000-0000-4000-8000-%012x",
        token
    )
    return instance_id, namespace .. ":" .. instance_id
end

local function is_canonical_uuid4(instance_id)
    if type(instance_id) ~= "string"
        or string.len(instance_id) ~= 36
        or string.lower(instance_id) ~= instance_id then
        return false
    end
    if string.sub(instance_id, 9, 9) ~= "-"
        or string.sub(instance_id, 14, 14) ~= "-"
        or string.sub(instance_id, 19, 19) ~= "-"
        or string.sub(instance_id, 24, 24) ~= "-"
        or string.sub(instance_id, 15, 15) ~= "4" then
        return false
    end
    local variant = string.sub(instance_id, 20, 20)
    if variant ~= "8"
        and variant ~= "9"
        and variant ~= "a"
        and variant ~= "b" then
        return false
    end
    local compact = string.gsub(instance_id, "-", "")
    return string.len(compact) == 32
        and string.match(compact, "^[0-9a-f]+$") ~= nil
end

local function record_persistence_generation(current, fencing_token)
    local instance_id = current["persistence_instance_id"]
    local persistence_namespace = current["persistence_namespace"]
    local has_instance_id = instance_id ~= nil
    local has_persistence_namespace = persistence_namespace ~= nil
    if not has_instance_id and not has_persistence_namespace then
        return legacy_persistence_generation(fencing_token)
    end
    local namespace_prefix = namespace .. ":"
    if not has_instance_id then
        if type(persistence_namespace) ~= "string"
            or string.sub(
                persistence_namespace,
                1,
                string.len(namespace_prefix)
            ) ~= namespace_prefix then
            return nil, nil
        end
        instance_id = string.sub(
            persistence_namespace,
            string.len(namespace_prefix) + 1
        )
    end
    if not is_canonical_uuid4(instance_id) then
        return nil, nil
    end
    local expected_namespace = namespace_prefix .. instance_id
    if has_persistence_namespace
        and persistence_namespace ~= expected_namespace then
        return nil, nil
    end
    return instance_id, expected_namespace
end

if not redis_fencing_epoch then
    return {0, "MISSING_EPOCH", server_time}
end
if not is_canonical_uuid4(redis_fencing_epoch) then
    return {0, "CORRUPT_EPOCH", server_time}
end
if not max_age_seconds
    or max_age_seconds <= 0
    or max_age_seconds ~= math.floor(max_age_seconds) then
    return {0, "INVALID_MAX_AGE", server_time}
end
local fresh_after_epoch = server_time - max_age_seconds

if current_json then
    local decoded, current = pcall(cjson.decode, current_json)
    if not decoded or type(current) ~= "table" then
        return {0, "CORRUPT", server_time}
    end
    local fencing_token = decode_fencing_token(
        current["fencing_token"]
    )
    local current_redis_fencing_epoch = current["redis_fencing_epoch"]
    if not is_canonical_uuid4(current_redis_fencing_epoch) then
        return {0, "CORRUPT_EPOCH", server_time}
    end
    if current_redis_fencing_epoch ~= redis_fencing_epoch then
        return {0, "EPOCH_MISMATCH", server_time}
    end
    local instance_id, persistence_namespace = record_persistence_generation(
        current,
        fencing_token
    )
    if current["namespace"] ~= namespace
        or type(current["owner"]) ~= "string"
        or current["owner"] == ""
        or type(current["release_id"]) ~= "string"
        or current["release_id"] == ""
        or not fencing_token
        or not instance_id
        or not tonumber(current["refreshed_at_epoch"]) then
        return {0, "CORRUPT", server_time}
    end
    local refreshed_at_epoch = tonumber(current["refreshed_at_epoch"])
    if not current_score
        or tonumber(current_score) ~= refreshed_at_epoch then
        return {0, "CORRUPT", server_time}
    end
    if refreshed_at_epoch > fresh_after_epoch then
        return {0, "FRESH", server_time}
    end
    redis.call("HDEL", metadata_key, namespace)
    redis.call("ZREM", registry_key, namespace)
    return {1, "REMOVED_STALE", server_time}
end

if not current_score then
    return {0, "MISSING", server_time}
end
return {0, "MISSING_EPOCH_METADATA", server_time}
"""


class RedisNamespaceLeaseError(RuntimeError):
    pass


class RedisNamespaceLeaseLost(RedisNamespaceLeaseError):
    pass


def derive_persistence_instance_id(fencing_token: int) -> str:
    _require_generation_fencing_token(fencing_token)
    return f"00000000-0000-4000-8000-{fencing_token:012x}"


def derive_persistence_namespace(namespace: str, fencing_token: int) -> str:
    stable_namespace = _required_identity("namespace", namespace).rstrip(":")
    if not stable_namespace:
        raise RedisNamespaceLeaseError("namespace must be non-empty")
    instance_id = derive_persistence_instance_id(fencing_token)
    return _persistence_namespace_from_instance_id(
        stable_namespace,
        instance_id,
    )


class RedisNamespaceLeaseClient(Protocol):
    def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> object:
        ...

    def zrangebyscore(
        self,
        key: str,
        minimum: int,
        maximum: str,
        *,
        withscores: bool,
    ) -> Iterable[tuple[bytes | str, float]]:
        ...

    def hget(self, key: str, field: str) -> bytes | str | None:
        ...

    def zscore(self, key: str, member: str) -> float | None:
        ...


class RedisServerTimeClient(Protocol):
    def time(self) -> tuple[int, int]:
        ...


@dataclass(frozen=True)
class RedisNamespaceLeaseRecord:
    namespace: str
    owner: str
    release_id: str
    fencing_token: int
    persistence_instance_id: str
    persistence_namespace: str
    refreshed_at_epoch: int
    redis_fencing_epoch: str = ""


class RedisNamespaceLease:
    def __init__(
        self,
        redis_client: RedisNamespaceLeaseClient,
        *,
        namespace: str,
        owner: str,
        release_id: str,
        registry_key: str = DEFAULT_REDIS_NAMESPACE_REGISTRY_KEY,
        max_age_seconds: int = DEFAULT_REDIS_NAMESPACE_LEASE_MAX_AGE_SECONDS,
        clock_fn: Callable[[], float] = time.time,
        persistence_instance_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._redis = redis_client
        self._namespace = _required_identity("namespace", namespace).rstrip(":")
        self._owner = _required_identity("owner", owner)
        self._release_id = _required_identity("release_id", release_id)
        self._registry_key = _required_identity("registry_key", registry_key)
        _require_positive("max_age_seconds", max_age_seconds)
        self._max_age_seconds = max_age_seconds
        self._clock_fn = clock_fn
        factory = persistence_instance_id_factory
        if factory is None:
            factory = _generate_persistence_instance_id
        if not callable(factory):
            raise TypeError("persistence_instance_id_factory must be callable")
        self._persistence_instance_id_factory = factory
        self._acquisition_candidate: str | bool = False
        self._record: RedisNamespaceLeaseRecord | bool = False

    @property
    def record(self) -> RedisNamespaceLeaseRecord | bool:
        return self._record

    def acquire(self) -> RedisNamespaceLeaseRecord:
        candidate = self._acquisition_candidate
        if candidate is False:
            candidate = _canonical_persistence_instance_id(
                self._persistence_instance_id_factory()
            )
            self._acquisition_candidate = candidate
        record = acquire_namespace_lease(
            self._redis,
            namespace=self._namespace,
            owner=self._owner,
            release_id=self._release_id,
            registry_key=self._registry_key,
            max_age_seconds=self._max_age_seconds,
            persistence_instance_id_candidate=candidate,
        )
        self._acquisition_candidate = False
        self._record = record
        return record

    def refresh(self) -> RedisNamespaceLeaseRecord:
        record = self._record
        if record is False:
            raise RedisNamespaceLeaseLost("namespace lease has not been acquired")
        refreshed = refresh_namespace_lease(
            self._redis,
            record=record,
            registry_key=self._registry_key,
            max_age_seconds=self._max_age_seconds,
        )
        if (
            refreshed.persistence_instance_id
            != record.persistence_instance_id
            or refreshed.persistence_namespace
            != record.persistence_namespace
            or refreshed.redis_fencing_epoch
            != record.redis_fencing_epoch
        ):
            raise RedisNamespaceLeaseLost(
                "namespace lease refresh changed persistence generation"
            )
        self._record = refreshed
        return refreshed

    def release(self) -> bool:
        record = self._record
        if record is False:
            return False
        removed = remove_namespace_lease(
            self._redis,
            record=record,
            registry_key=self._registry_key,
        )
        if removed:
            self._record = False
        return removed


class NamespaceLeaseGuard:
    process_owned = True

    def __init__(
        self,
        lease: Any,
        *,
        refresh_interval_seconds: float = (
            DEFAULT_REDIS_NAMESPACE_LEASE_REFRESH_INTERVAL_SECONDS
        ),
        thread_join_timeout_seconds: float = (
            DEFAULT_REDIS_NAMESPACE_LEASE_THREAD_JOIN_SECONDS
        ),
        lease_lost_callback: Callable[[str], None] | None = None,
    ) -> None:
        if refresh_interval_seconds <= 0:
            raise ValueError("refresh_interval_seconds must be positive")
        if thread_join_timeout_seconds <= 0:
            raise ValueError("thread_join_timeout_seconds must be positive")
        self._lease = lease
        self._refresh_interval_seconds = float(refresh_interval_seconds)
        self._thread_join_timeout_seconds = float(thread_join_timeout_seconds)
        self._callbacks: list[Callable[[str], None]] = []
        if lease_lost_callback is not None:
            self._callbacks.append(lease_lost_callback)
        self._lock = RLock()
        self._stop = Event()
        self._thread: Thread | bool = False
        self._record: Any = False
        self._failure_reason = ""
        self._closed = False

    @property
    def record(self) -> Any:
        with self._lock:
            return self._record

    @property
    def failure_reason(self) -> str:
        with self._lock:
            return self._failure_reason

    @property
    def has_failure_callbacks(self) -> bool:
        with self._lock:
            return bool(self._callbacks)

    @property
    def is_healthy(self) -> bool:
        with self._lock:
            return (
                self._record is not False
                and not self._failure_reason
                and not self._closed
            )

    @property
    def is_running(self) -> bool:
        with self._lock:
            thread = self._thread
        return isinstance(thread, Thread) and thread.is_alive()

    def add_lease_lost_callback(
        self,
        callback: Callable[[str], None],
    ) -> None:
        if not callable(callback):
            raise TypeError("lease lost callback must be callable")
        with self._lock:
            if self._closed:
                raise RedisNamespaceLeaseError("namespace lease guard is closed")
            self._callbacks.append(callback)

    def acquire(self) -> Any:
        with self._lock:
            if self._closed:
                raise RedisNamespaceLeaseError("namespace lease guard is closed")
            if self._failure_reason:
                raise RedisNamespaceLeaseLost(self._failure_reason)
            if self._record is not False:
                return self._record
        record = self._lease.acquire()
        with self._lock:
            if self._closed:
                self._lease.release()
                raise RedisNamespaceLeaseError(
                    "namespace lease guard closed during acquisition"
                )
            self._record = record
            thread = Thread(
                target=self._renewal_loop,
                name="redis-namespace-lease-guard",
                daemon=True,
            )
            self._thread = thread
        try:
            thread.start()
        except Exception as exc:
            with self._lock:
                self._closed = True
                self._stop.set()
                self._thread = False
                self._record = False
            try:
                self._lease.release()
            except Exception as release_exc:
                raise RedisNamespaceLeaseError(
                    "namespace lease renewal thread failed to start and "
                    "lease cleanup failed"
                ) from release_exc
            raise RedisNamespaceLeaseError(
                "namespace lease renewal thread failed to start"
            ) from exc
        return record

    def refresh(self) -> Any:
        with self._lock:
            if self._closed:
                raise RedisNamespaceLeaseLost("namespace lease guard is closed")
            if self._failure_reason:
                raise RedisNamespaceLeaseLost(self._failure_reason)
            if self._record is False:
                raise RedisNamespaceLeaseLost(
                    "namespace lease guard has not been acquired"
                )
        record = self._lease.refresh()
        with self._lock:
            if self._closed:
                return record
            self._record = record
            return record

    def close(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._closed = True
            self._stop.set()
            thread = self._thread

        thread_alive = False
        if isinstance(thread, Thread) and thread is not current_thread():
            thread.join(timeout=self._thread_join_timeout_seconds)
            thread_alive = thread.is_alive()

        release_error: Exception | bool = False
        released = False
        try:
            released = bool(self._lease.release())
        except Exception as exc:
            release_error = exc
        finally:
            with self._lock:
                self._record = False

        if release_error is not False:
            raise RedisNamespaceLeaseError(
                "namespace lease guard release failed"
            ) from release_error
        if thread_alive:
            raise RedisNamespaceLeaseError(
                "namespace lease renewal thread did not stop before release"
            )
        return released

    def release(self) -> bool:
        return self.close()

    def _renewal_loop(self) -> None:
        while not self._stop.wait(self._refresh_interval_seconds):
            try:
                self.refresh()
            except Exception as exc:
                self._record_failure(exc)
                return

    def _record_failure(self, exc: Exception) -> None:
        detail = str(exc).strip()
        if not detail:
            detail = type(exc).__name__
        reason = f"Redis namespace lease refresh failed: {detail}"
        with self._lock:
            if self._closed or self._failure_reason:
                return
            self._failure_reason = reason
            self._stop.set()
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            try:
                callback(reason)
            except Exception as callback_exc:
                print(
                    "[NamespaceLeaseGuard] lease lost callback failed: "
                    f"{callback_exc!r}",
                    flush=True,
                )


def acquire_namespace_lease(
    redis_client: RedisNamespaceLeaseClient,
    *,
    namespace: str,
    owner: str,
    release_id: str,
    registry_key: str = DEFAULT_REDIS_NAMESPACE_REGISTRY_KEY,
    refreshed_at_epoch: int | None = None,
    max_age_seconds: int = DEFAULT_REDIS_NAMESPACE_LEASE_MAX_AGE_SECONDS,
    persistence_instance_id_candidate: str | None = None,
) -> RedisNamespaceLeaseRecord:
    normalized_namespace = _required_identity("namespace", namespace).rstrip(":")
    normalized_owner = _required_identity("owner", owner)
    normalized_release_id = _required_identity("release_id", release_id)
    normalized_registry_key = _required_identity("registry_key", registry_key)
    if refreshed_at_epoch is not None:
        _require_positive("refreshed_at_epoch", refreshed_at_epoch)
    _require_positive("max_age_seconds", max_age_seconds)
    candidate = persistence_instance_id_candidate
    if candidate is None:
        candidate = _generate_persistence_instance_id()
    normalized_candidate = _canonical_persistence_instance_id(candidate)
    raw_result = redis_client.eval(
        _ACQUIRE_LUA,
        4,
        normalized_registry_key,
        _metadata_key(normalized_registry_key),
        _fencing_key(normalized_registry_key),
        DEFAULT_REDIS_FENCING_EPOCH_KEY,
        normalized_namespace,
        normalized_owner,
        normalized_release_id,
        max_age_seconds,
        normalized_candidate,
    )
    (
        fencing_token,
        recorded_epoch,
        persistence_instance_id,
        persistence_namespace,
        redis_fencing_epoch,
    ) = _acquire_result(
        raw_result,
        expected_namespace=normalized_namespace,
    )
    return RedisNamespaceLeaseRecord(
        namespace=normalized_namespace,
        owner=normalized_owner,
        release_id=normalized_release_id,
        fencing_token=fencing_token,
        persistence_instance_id=persistence_instance_id,
        persistence_namespace=persistence_namespace,
        refreshed_at_epoch=recorded_epoch,
        redis_fencing_epoch=redis_fencing_epoch,
    )


def refresh_namespace_lease(
    redis_client: RedisNamespaceLeaseClient,
    *,
    record: RedisNamespaceLeaseRecord,
    registry_key: str = DEFAULT_REDIS_NAMESPACE_REGISTRY_KEY,
    refreshed_at_epoch: int | None = None,
    max_age_seconds: int = DEFAULT_REDIS_NAMESPACE_LEASE_MAX_AGE_SECONDS,
) -> RedisNamespaceLeaseRecord:
    _require_record(record)
    normalized_registry_key = _required_identity("registry_key", registry_key)
    if refreshed_at_epoch is not None:
        _require_positive("refreshed_at_epoch", refreshed_at_epoch)
    _require_positive("max_age_seconds", max_age_seconds)
    raw_result = redis_client.eval(
        _REFRESH_LUA,
        3,
        normalized_registry_key,
        _metadata_key(normalized_registry_key),
        DEFAULT_REDIS_FENCING_EPOCH_KEY,
        record.namespace,
        record.owner,
        record.release_id,
        record.fencing_token,
        max_age_seconds,
        record.redis_fencing_epoch,
    )
    (
        recorded_epoch,
        persistence_instance_id,
        persistence_namespace,
        redis_fencing_epoch,
    ) = _refresh_result(
        raw_result,
        expected_namespace=record.namespace,
    )
    return RedisNamespaceLeaseRecord(
        namespace=record.namespace,
        owner=record.owner,
        release_id=record.release_id,
        fencing_token=record.fencing_token,
        persistence_instance_id=persistence_instance_id,
        persistence_namespace=persistence_namespace,
        refreshed_at_epoch=recorded_epoch,
        redis_fencing_epoch=redis_fencing_epoch,
    )


def remove_namespace_lease(
    redis_client: RedisNamespaceLeaseClient,
    *,
    record: RedisNamespaceLeaseRecord,
    registry_key: str = DEFAULT_REDIS_NAMESPACE_REGISTRY_KEY,
) -> bool:
    normalized_registry_key = _required_identity("registry_key", registry_key)
    return _remove_namespace_lease_record(
        redis_client,
        record=record,
        registry_key=normalized_registry_key,
    )


def force_remove_stale_namespace_lease(
    redis_client: RedisNamespaceLeaseClient,
    namespace: str,
    *,
    registry_key: str = DEFAULT_REDIS_NAMESPACE_REGISTRY_KEY,
    max_age_seconds: int = DEFAULT_REDIS_NAMESPACE_LEASE_MAX_AGE_SECONDS,
) -> bool:
    normalized_namespace = _required_identity("namespace", namespace).rstrip(":")
    normalized_registry_key = _required_identity("registry_key", registry_key)
    _require_positive("max_age_seconds", max_age_seconds)
    raw_result = redis_client.eval(
        _FORCE_REMOVE_STALE_LUA,
        3,
        normalized_registry_key,
        _metadata_key(normalized_registry_key),
        DEFAULT_REDIS_FENCING_EPOCH_KEY,
        normalized_namespace,
        max_age_seconds,
    )
    accepted, status, server_time = _force_remove_result(raw_result)
    if accepted == 1 and status in {
        "REMOVED_STALE",
        "REMOVED_STALE_LEGACY",
    }:
        return True
    if accepted == 0 and status == "MISSING":
        return False
    raise RedisNamespaceLeaseError(
        "stale namespace lease removal failed closed: "
        f"{status.lower()} at redis time {server_time}"
    )


def _remove_namespace_lease_record(
    redis_client: RedisNamespaceLeaseClient,
    *,
    record: RedisNamespaceLeaseRecord,
    registry_key: str,
) -> bool:
    _require_record(record)
    raw_result = redis_client.eval(
        _REMOVE_LUA,
        3,
        registry_key,
        _metadata_key(registry_key),
        DEFAULT_REDIS_FENCING_EPOCH_KEY,
        record.namespace,
        record.owner,
        record.release_id,
        record.fencing_token,
        record.redis_fencing_epoch,
    )
    accepted, status = _lease_result(raw_result, operation="remove")
    return status == "REMOVED" and accepted > 0


def remove_current_namespace_lease(
    redis_client: RedisNamespaceLeaseClient,
    namespace: str,
    *,
    registry_key: str = DEFAULT_REDIS_NAMESPACE_REGISTRY_KEY,
) -> bool:
    del redis_client, namespace, registry_key
    raise RedisNamespaceLeaseError(
        "identity-free namespace lease removal is disabled; "
        "provide owner, release_id, and fencing_token or use "
        "force_remove_stale_namespace_lease"
    )


def get_namespace_lease(
    redis_client: RedisNamespaceLeaseClient,
    namespace: str,
    *,
    registry_key: str = DEFAULT_REDIS_NAMESPACE_REGISTRY_KEY,
) -> RedisNamespaceLeaseRecord | bool:
    normalized_namespace = _required_identity("namespace", namespace).rstrip(":")
    normalized_registry_key = _required_identity("registry_key", registry_key)
    raw_record = redis_client.hget(
        _metadata_key(normalized_registry_key),
        normalized_namespace,
    )
    if raw_record is None:
        return False
    return _decode_record(raw_record, expected_namespace=normalized_namespace)


def list_active_namespace_leases(
    redis_client: RedisNamespaceLeaseClient,
    *,
    fresh_after_epoch: int,
    registry_key: str = DEFAULT_REDIS_NAMESPACE_REGISTRY_KEY,
) -> tuple[RedisNamespaceLeaseRecord, ...]:
    _require_positive("fresh_after_epoch", fresh_after_epoch)
    normalized_registry_key = _required_identity("registry_key", registry_key)
    members = redis_client.zrangebyscore(
        normalized_registry_key,
        fresh_after_epoch,
        "+inf",
        withscores=True,
    )
    records: list[RedisNamespaceLeaseRecord] = []
    for raw_namespace, _raw_score in members:
        namespace = _decode(raw_namespace)
        record = _read_listed_namespace_lease(
            redis_client,
            namespace,
            registry_key=normalized_registry_key,
        )
        if record is False:
            continue
        records.append(record)
    return tuple(sorted(records, key=lambda item: item.namespace))


def _read_listed_namespace_lease(
    redis_client: RedisNamespaceLeaseClient,
    namespace: str,
    *,
    registry_key: str,
) -> RedisNamespaceLeaseRecord | bool:
    metadata_key = _metadata_key(registry_key)
    for _attempt in range(DEFAULT_REDIS_NAMESPACE_LIST_SNAPSHOT_ATTEMPTS):
        raw_record_before = redis_client.hget(metadata_key, namespace)
        raw_score = redis_client.zscore(registry_key, namespace)
        raw_record_after = redis_client.hget(metadata_key, namespace)
        if raw_record_before != raw_record_after:
            continue
        if raw_score is None:
            if raw_record_before is None:
                return False
            continue
        score = _redis_score_epoch(raw_score)
        if raw_record_before is None:
            raise RedisNamespaceLeaseError(
                "namespace lease metadata is invalid"
            )
        record = _decode_record(
            raw_record_before,
            expected_namespace=namespace,
        )
        if record.refreshed_at_epoch == score:
            return record
    raise RedisNamespaceLeaseError(
        "namespace lease snapshot changed during listing"
    )


def _redis_score_epoch(raw_score: object) -> int:
    try:
        score = float(raw_score)
    except (TypeError, ValueError) as exc:
        raise RedisNamespaceLeaseError(
            "namespace lease registry score is invalid"
        ) from exc
    if score <= 0 or not score.is_integer():
        raise RedisNamespaceLeaseError(
            "namespace lease registry score is invalid"
        )
    return int(score)


def redis_server_time_epoch(
    redis_client: RedisServerTimeClient,
) -> int:
    try:
        raw_time = redis_client.time()
        server_time = int(raw_time[0])
    except (AttributeError, IndexError, TypeError, ValueError) as exc:
        raise RedisNamespaceLeaseError(
            "Redis server time response is invalid"
        ) from exc
    _require_positive("redis server time", server_time)
    return server_time


def _metadata_key(registry_key: str) -> str:
    return f"{registry_key}:leases"


def _fencing_key(registry_key: str) -> str:
    return f"{registry_key}:fencing"


def _decode_record(
    raw_record: bytes | str,
    *,
    expected_namespace: str,
    expected_refreshed_at_epoch: int | None = None,
) -> RedisNamespaceLeaseRecord:
    try:
        payload = json.loads(_decode(raw_record))
        namespace = str(payload["namespace"])
        redis_fencing_epoch = _canonical_redis_fencing_epoch(
            payload["redis_fencing_epoch"]
        )
        fencing_token = _decode_record_fencing_token(
            payload["fencing_token"]
        )
        (
            persistence_instance_id,
            persistence_namespace,
        ) = _persistence_generation_from_payload(
            payload,
            namespace,
            fencing_token,
        )
        record = RedisNamespaceLeaseRecord(
            namespace=namespace,
            owner=str(payload["owner"]),
            release_id=str(payload["release_id"]),
            fencing_token=fencing_token,
            persistence_instance_id=persistence_instance_id,
            persistence_namespace=persistence_namespace,
            refreshed_at_epoch=int(payload["refreshed_at_epoch"]),
            redis_fencing_epoch=redis_fencing_epoch,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        RedisNamespaceLeaseError,
    ) as exc:
        raise RedisNamespaceLeaseError(
            "namespace lease metadata is invalid"
        ) from exc
    _require_record(record)
    if record.namespace != expected_namespace:
        raise RedisNamespaceLeaseError(
            "namespace lease metadata identity mismatch"
        )
    if (
        expected_refreshed_at_epoch is not None
        and record.refreshed_at_epoch != expected_refreshed_at_epoch
    ):
        raise RedisNamespaceLeaseError(
            "namespace lease freshness metadata mismatch"
        )
    return record


def _acquire_result(
    raw_result: object,
    *,
    expected_namespace: str,
) -> tuple[int, int, str, str, str]:
    if not isinstance(raw_result, (list, tuple)):
        raise RedisNamespaceLeaseError(
            "namespace lease acquire returned an invalid result"
        )
    if len(raw_result) not in {3, 6}:
        raise RedisNamespaceLeaseError(
            "namespace lease acquire returned an invalid result"
        )
    try:
        accepted = int(raw_result[0])
    except (TypeError, ValueError) as exc:
        raise RedisNamespaceLeaseError(
            "namespace lease acquire returned an invalid acceptance flag"
        ) from exc
    if accepted == 0:
        if len(raw_result) != 3:
            raise RedisNamespaceLeaseError(
                "namespace lease acquire returned an invalid rejection"
            )
        status = _decode(raw_result[2])
        raise RedisNamespaceLeaseLost(
            f"namespace lease acquire failed closed: {status.lower()}"
        )
    if accepted != 1:
        raise RedisNamespaceLeaseError(
            "namespace lease acquire returned an invalid acceptance flag"
        )
    if len(raw_result) != 6:
        raise RedisNamespaceLeaseError(
            "namespace lease acquire returned incomplete generation metadata"
        )
    try:
        fencing_token = int(raw_result[1])
        refreshed_at_epoch = int(raw_result[2])
    except (TypeError, ValueError) as exc:
        raise RedisNamespaceLeaseError(
            "namespace lease acquire returned invalid integers"
        ) from exc
    _require_generation_fencing_token(fencing_token)
    _require_positive("refreshed_at_epoch", refreshed_at_epoch)
    persistence_instance_id = _canonical_persistence_instance_id(
        _decode(raw_result[3])
    )
    persistence_namespace = _decode(raw_result[4])
    redis_fencing_epoch = _canonical_redis_fencing_epoch(
        _decode(raw_result[5])
    )
    expected_persistence_namespace = _persistence_namespace_from_instance_id(
        expected_namespace,
        persistence_instance_id,
    )
    if persistence_namespace != expected_persistence_namespace:
        raise RedisNamespaceLeaseError(
            "namespace lease acquire generation identity mismatch"
        )
    return (
        fencing_token,
        refreshed_at_epoch,
        persistence_instance_id,
        persistence_namespace,
        redis_fencing_epoch,
    )


def _refresh_result(
    raw_result: object,
    *,
    expected_namespace: str,
) -> tuple[int, str, str, str]:
    if not isinstance(raw_result, (list, tuple)):
        raise RedisNamespaceLeaseError(
            "namespace lease refresh returned an invalid result"
        )
    if len(raw_result) not in {3, 6}:
        raise RedisNamespaceLeaseError(
            "namespace lease refresh returned an invalid result"
        )
    try:
        accepted = int(raw_result[0])
        refreshed_at_epoch = int(raw_result[2])
    except (TypeError, ValueError) as exc:
        raise RedisNamespaceLeaseError(
            "namespace lease refresh returned invalid integers"
        ) from exc
    status = _decode(raw_result[1])
    _require_positive("redis server time", refreshed_at_epoch)
    if accepted == 0:
        if len(raw_result) != 3:
            raise RedisNamespaceLeaseError(
                "namespace lease refresh returned an invalid rejection"
            )
        raise RedisNamespaceLeaseLost(
            f"namespace lease refresh failed closed: {status.lower()}"
        )
    if accepted != 1 or status != "REFRESHED":
        raise RedisNamespaceLeaseError(
            "namespace lease refresh returned an invalid acceptance"
        )
    if len(raw_result) != 6:
        raise RedisNamespaceLeaseError(
            "namespace lease refresh returned incomplete generation metadata"
        )
    persistence_instance_id = _canonical_persistence_instance_id(
        _decode(raw_result[3])
    )
    persistence_namespace = _decode(raw_result[4])
    redis_fencing_epoch = _canonical_redis_fencing_epoch(
        _decode(raw_result[5])
    )
    expected_persistence_namespace = _persistence_namespace_from_instance_id(
        expected_namespace,
        persistence_instance_id,
    )
    if persistence_namespace != expected_persistence_namespace:
        raise RedisNamespaceLeaseError(
            "namespace lease refresh generation identity mismatch"
        )
    return (
        refreshed_at_epoch,
        persistence_instance_id,
        persistence_namespace,
        redis_fencing_epoch,
    )


def _lease_result(raw_result: object, *, operation: str) -> tuple[int, str]:
    if not isinstance(raw_result, (list, tuple)) or len(raw_result) != 2:
        raise RedisNamespaceLeaseError(
            f"namespace lease {operation} returned an invalid result"
        )
    try:
        accepted = int(raw_result[0])
    except (TypeError, ValueError) as exc:
        raise RedisNamespaceLeaseError(
            f"namespace lease {operation} returned an invalid acceptance flag"
        ) from exc
    return accepted, _decode(raw_result[1])


def _force_remove_result(raw_result: object) -> tuple[int, str, int]:
    if not isinstance(raw_result, (list, tuple)) or len(raw_result) != 3:
        raise RedisNamespaceLeaseError(
            "stale namespace lease removal returned an invalid result"
        )
    try:
        accepted = int(raw_result[0])
        server_time = int(raw_result[2])
    except (TypeError, ValueError) as exc:
        raise RedisNamespaceLeaseError(
            "stale namespace lease removal returned invalid integers"
        ) from exc
    if accepted not in {0, 1}:
        raise RedisNamespaceLeaseError(
            "stale namespace lease removal returned an invalid acceptance flag"
        )
    _require_positive("redis server time", server_time)
    return accepted, _decode(raw_result[1]), server_time


def _require_record(record: RedisNamespaceLeaseRecord) -> None:
    namespace = _required_identity("namespace", record.namespace)
    if namespace.rstrip(":") != namespace:
        raise RedisNamespaceLeaseError(
            "namespace lease record identity mismatch"
        )
    _required_identity("owner", record.owner)
    _required_identity("release_id", record.release_id)
    _canonical_redis_fencing_epoch(record.redis_fencing_epoch)
    _require_generation_fencing_token(record.fencing_token)
    persistence_instance_id = _canonical_persistence_instance_id(
        record.persistence_instance_id
    )
    expected_persistence_namespace = _persistence_namespace_from_instance_id(
        namespace,
        persistence_instance_id,
    )
    if record.persistence_namespace != expected_persistence_namespace:
        raise RedisNamespaceLeaseError(
            "namespace lease record identity mismatch"
        )
    _require_positive("refreshed_at_epoch", record.refreshed_at_epoch)


def _persistence_generation_from_payload(
    payload: dict[str, object],
    namespace: str,
    fencing_token: int,
) -> tuple[str, str]:
    has_instance_id = "persistence_instance_id" in payload
    has_persistence_namespace = "persistence_namespace" in payload
    if not has_instance_id and not has_persistence_namespace:
        instance_id = derive_persistence_instance_id(fencing_token)
        return (
            instance_id,
            _persistence_namespace_from_instance_id(namespace, instance_id),
        )

    instance_id = ""
    if has_instance_id:
        raw_instance_id = payload["persistence_instance_id"]
        if not isinstance(raw_instance_id, str):
            raise ValueError("persistence_instance_id must be a string")
        instance_id = _canonical_persistence_instance_id(raw_instance_id)

    persistence_namespace = ""
    if has_persistence_namespace:
        raw_persistence_namespace = payload["persistence_namespace"]
        if not isinstance(raw_persistence_namespace, str):
            raise ValueError("persistence_namespace must be a string")
        persistence_namespace = raw_persistence_namespace

    if not instance_id:
        prefix = f"{namespace}:"
        if not persistence_namespace.startswith(prefix):
            raise ValueError("persistence_namespace identity mismatch")
        instance_id = _canonical_persistence_instance_id(
            persistence_namespace[len(prefix) :]
        )

    expected_namespace = _persistence_namespace_from_instance_id(
        namespace,
        instance_id,
    )
    if persistence_namespace and persistence_namespace != expected_namespace:
        raise ValueError("persistence_namespace identity mismatch")
    return instance_id, expected_namespace


def _generate_persistence_instance_id() -> str:
    return str(uuid4())


def _canonical_persistence_instance_id(value: str) -> str:
    if not isinstance(value, str):
        raise RedisNamespaceLeaseError(
            "persistence_instance_id must be a canonical UUID4 string"
        )
    if value != value.strip() or value != value.lower():
        raise RedisNamespaceLeaseError(
            "persistence_instance_id must be a canonical UUID4 string"
        )
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise RedisNamespaceLeaseError(
            "persistence_instance_id must be a canonical UUID4 string"
        ) from exc
    if parsed.version != 4 or str(parsed) != value:
        raise RedisNamespaceLeaseError(
            "persistence_instance_id must be a canonical UUID4 string"
        )
    return value


def _canonical_redis_fencing_epoch(value: object) -> str:
    if not isinstance(value, str):
        raise RedisNamespaceLeaseError(
            "redis_fencing_epoch must be a canonical UUID4 string"
        )
    if value != value.strip() or value != value.lower():
        raise RedisNamespaceLeaseError(
            "redis_fencing_epoch must be a canonical UUID4 string"
        )
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise RedisNamespaceLeaseError(
            "redis_fencing_epoch must be a canonical UUID4 string"
        ) from exc
    if parsed.version != 4 or str(parsed) != value:
        raise RedisNamespaceLeaseError(
            "redis_fencing_epoch must be a canonical UUID4 string"
        )
    return value


def _persistence_namespace_from_instance_id(
    namespace: str,
    persistence_instance_id: str,
) -> str:
    stable_namespace = _required_identity("namespace", namespace).rstrip(":")
    if not stable_namespace:
        raise RedisNamespaceLeaseError("namespace must be non-empty")
    instance_id = _canonical_persistence_instance_id(
        persistence_instance_id
    )
    return f"{stable_namespace}:{instance_id}"


def _required_identity(label: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise RedisNamespaceLeaseError(f"{label} must be non-empty")
    return normalized


def _require_positive(label: str, value: int) -> None:
    if value <= 0:
        raise RedisNamespaceLeaseError(f"{label} must be positive")


def _require_generation_fencing_token(fencing_token: int) -> None:
    if isinstance(fencing_token, bool) or not isinstance(fencing_token, int):
        raise RedisNamespaceLeaseError("fencing_token must be an integer")
    if fencing_token <= 0:
        raise RedisNamespaceLeaseError("fencing_token must be positive")
    if fencing_token > MAX_PERSISTENCE_GENERATION_FENCING_TOKEN:
        raise RedisNamespaceLeaseError(
            "fencing_token exceeds the persistence generation limit"
        )


def _decode_record_fencing_token(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("fencing_token must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if not _ASCII_DECIMAL_PATTERN.fullmatch(value):
            raise ValueError("fencing_token must be an integer")
        return int(value)
    raise ValueError("fencing_token must be an integer")


def _decode(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value
