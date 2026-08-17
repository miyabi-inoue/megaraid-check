%define         base_name       megaraid-check
%define         pkg_version     1.0.0
%define         pkg_release     1%{?dist}

Name:           %{base_name}
Version:        %{pkg_version}
Release:        %{pkg_release}
Summary:        MegaRAID and SMART health monitoring tool for Linux

License:        MIT
URL:            https://github.com/miyabi-inoue/megaraid-check
Source0:        %{base_name}-%{version}.tar.gz

BuildArch:      noarch
Requires:       python3, smartmontools, util-linux, systemd

Packager:       miyabi

%description
megaraid-check periodically checks MegaRAID logical and physical
drive status, SMART attributes, and the MegaRAID event log.

It detects changes in error counters and sends email notifications
when anomalies are found. Alert messages include drive model,
serial number, and WWN whenever available.

%description -l ja
megaraid-check は MegaRAID 配下の論理・物理ディスク状態、
SMART 属性、MegaRAID Event Log を定期確認する監視ツールです。

エラーカウンタの増加や異常状態を検出し、必要に応じてメール通知します。
警告には可能な限りモデル名、シリアル番号、WWNを含め、
交換対象ディスクを特定しやすくします。

%prep
%autosetup

%build
# Nothing to build

%install
install -Dpm 0755 megaraid_check.py %{buildroot}%{_sbindir}/megaraid-check

install -Dpm 0644 megaraid_check.conf.example %{buildroot}%{_sysconfdir}/%{base_name}/megaraid_check.conf

install -Dpm 0644 megaraid-check.service %{buildroot}%{_unitdir}/megaraid-check.service
install -Dpm 0644 megaraid-check.timer %{buildroot}%{_unitdir}/megaraid-check.timer

install -d -m 0700 %{buildroot}%{_localstatedir}/lib/%{base_name}

%post
systemctl daemon-reload >/dev/null 2>&1 || :

%preun
if [ $1 -eq 0 ]; then
    systemctl disable --now megaraid-check.timer >/dev/null 2>&1 || :
fi

%postun
systemctl daemon-reload >/dev/null 2>&1 || :

%files
%license LICENSE
%doc README.md

%{_sbindir}/megaraid-check

%dir %{_sysconfdir}/%{base_name}
%config(noreplace) %{_sysconfdir}/%{base_name}/megaraid_check.conf

%dir %{_localstatedir}/lib/megaraid-check

%{_unitdir}/megaraid-check.service
%{_unitdir}/megaraid-check.timer

%changelog
* Mon Aug 17 2026 miyabi <miyabi@program-laboratory.com> - 1.0.0-1
- Initial package
