function isEnabledByDefault(rawValue) {
  if (!rawValue) {
    return true;
  }

  const value = String(rawValue).trim().toLowerCase();
  return !/^(0|false|off|no)$/.test(value);
}

function isExplicitlyEnabled(rawValue) {
  if (!rawValue) {
    return false;
  }

  const value = String(rawValue).trim().toLowerCase();
  return /^(1|true|on|yes)$/.test(value);
}

module.exports = {
  isEnabledByDefault,
  isExplicitlyEnabled,
};
