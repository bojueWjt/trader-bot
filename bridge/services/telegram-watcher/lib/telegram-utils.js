function updateName(update) {
  if (update.className) {
    return update.className;
  }
  const constructorRef = update.constructor;
  if (constructorRef && constructorRef.name) {
    return constructorRef.name;
  }
  return typeof update;
}

function shortUpdateText(update) {
  const message = update.message;
  if (!message) {
    return "";
  }
  return message.substring(0, 50);
}

function documentMimeType(media) {
  if (!media) {
    return "";
  }
  const document = media.document;
  if (!document) {
    return "";
  }
  return document.mimeType || "";
}

function mediaExtension(media, fallback) {
  const mimeType = documentMimeType(media);
  if (!mimeType) {
    return fallback;
  }
  const parts = mimeType.split("/");
  if (parts.length < 2) {
    return fallback;
  }
  return parts[1] || fallback;
}

function webpageValue(media, key) {
  if (!media) {
    return "";
  }
  const webpage = media.webpage;
  if (!webpage) {
    return "";
  }
  return webpage[key] || "";
}

function chatIdFromPeerId(peerId) {
  if (!peerId) {
    return "";
  }

  if (peerId.channelId) {
    return `-100${peerId.channelId.toString()}`;
  }

  if (peerId.chatId) {
    return `-${peerId.chatId.toString()}`;
  }

  if (peerId.userId) {
    return peerId.userId.toString();
  }

  return "";
}

function chatIdFromEntity(entity, fallback) {
  if (!entity) {
    return canonicalWatchedChatId(fallback);
  }

  if (!entity.id) {
    return canonicalWatchedChatId(fallback);
  }

  const id = entity.id.toString();
  const name = updateName(entity);
  if (/Channel/.test(name)) {
    return `-100${id.replace(/^-100/, "").replace(/^-/, "")}`;
  }

  if (/Chat/.test(name)) {
    return `-${id.replace(/^-/, "")}`;
  }

  if (/User/.test(name)) {
    return id.replace(/^-/, "");
  }

  return canonicalWatchedChatId(fallback || id);
}

function canonicalWatchedChatId(value) {
  const rawValue = String(value || "");
  if (!rawValue) {
    return "";
  }

  if (/^-100\d+$/.test(rawValue)) {
    return rawValue;
  }

  if (/^-\d+$/.test(rawValue)) {
    return rawValue;
  }

  if (/^\d+$/.test(rawValue)) {
    return `-100${rawValue}`;
  }

  return rawValue;
}

function chatIdMatchesWatchList(chatId, watchGroups) {
  if (!Array.isArray(watchGroups)) {
    return false;
  }

  if (watchGroups.length === 0) {
    return true;
  }

  const watchedVariants = new Set();
  for (const groupId of watchGroups) {
    const variants = chatIdVariants(groupId);
    for (const variant of variants) {
      watchedVariants.add(variant);
    }
  }

  const currentVariants = chatIdVariants(chatId);
  for (const variant of currentVariants) {
    if (watchedVariants.has(variant)) {
      return true;
    }
  }

  return false;
}

function chatIdVariants(value) {
  const rawValue = String(value || "");
  const variants = new Set();
  if (!rawValue) {
    return variants;
  }

  variants.add(rawValue);

  const canonical = canonicalWatchedChatId(rawValue);
  if (canonical) {
    variants.add(canonical);
  }

  if (/^-100\d+$/.test(rawValue)) {
    const bareChannel = rawValue.replace(/^-100/, "");
    variants.add(bareChannel);
    variants.add(`-${bareChannel}`);
  } else if (/^-\d+$/.test(rawValue)) {
    const bareGroup = rawValue.replace(/^-/, "");
    variants.add(bareGroup);
  } else if (/^\d+$/.test(rawValue)) {
    variants.add(`-${rawValue}`);
    variants.add(`-100${rawValue}`);
  }

  return variants;
}

module.exports = {
  canonicalWatchedChatId,
  chatIdFromEntity,
  chatIdFromPeerId,
  chatIdMatchesWatchList,
  documentMimeType,
  mediaExtension,
  shortUpdateText,
  updateName,
  webpageValue,
};
