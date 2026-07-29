# 知识块来源说明

本文件对应 `data/knowledge/knowledge_chunks.jsonl`，记录知识块的来源与使用边界。知识块用于光网络故障根因解释、处置建议、结构化事件摘要和诊断输入组装。

## 主要来源

1. ITU-T G.652 (08/2024), Characteristics of a single-mode optical fibre and cable  
   URL: https://www.itu.int/epublications/fr/publication/itu-t-g-652-2024-08-characteristics-of-a-single-mode-optical-fibre-and-cable  
   用途：单模光纤传输属性、链路衰耗、色度色散、PMD、非线性与链路设计余量。

2. ITU-T G.694.1 (10/2020), Spectral grids for WDM applications: DWDM frequency grid  
   URL: https://www.itu.int/rec/T-REC-G.694.1-202010-I/en  
   用途：DWDM 频率栅格、通道间隔、波长漂移和频率偏移解释。

3. ITU-T G.694.2, Spectral grids for WDM applications: CWDM wavelength grid  
   URL: https://www.itu.int/rec/T-REC-G.694.2-200312-I/en  
   用途：CWDM 波长栅格、粗波分通道规划和模块波长不匹配解释。

4. ITU-T G.697, Optical monitoring for DWDM systems  
   URL: https://www.itu.int/ITU-T/recommendations/rec.aspx?lang=en&rec=11483  
   用途：光通道功率、波长、OSNR、OCM/OPM 光监测和增益变化解释。

5. ITU-T G.709/Y.1331, Interfaces for the optical transport network  
   URL: https://www.itu.int/epublications/en/publication/itu-t-g-709-y-1331-2020-amd-3-2024-03/en  
   用途：OTN 接口结构、OTU/ODU、FEC、BER 和客户业务承载关系。

6. ITU-T G.872 (03/2024), Architecture of the optical transport network  
   URL: https://www.itu.int/epublications/fr/publication/itu-t-g-872-2024-03-architecture-of-the-optical-transport-network  
   用途：OTN 功能架构、客户与服务层关系、拓扑和多层网络建模。

7. ITU-T G.798 (09/2023), Characteristics of optical transport network hierarchy equipment functional blocks  
   URL: https://www.itu.int/epublications/ar/publication/itu-t-g-798-2023-09-characteristics-of-optical-transport-network-hierarchy-equipment-functional-blocks/en  
   用途：OTN 设备功能块、监督、缺陷、告警控制和告警传播。

8. ITU-T G.959.1 (01/2024), Optical transport network physical layer interfaces  
   URL: https://www.itu.int/epublications/zh/publication/itu-t-g-959-1-2024-01-optical-transport-network-physical-layer-interfaces  
   用途：WDM/OTN 物理层接口、光接口兼容性和线路侧接口异常解释。

9. OpenConfig terminal-device / transceiver YANG documentation  
   URL: https://openconfig.net/projects/models/schemadocs/yangdoc/openconfig-terminal-device.html  
   用途：逻辑通道、OTN 状态、post-FEC BER、Q value、ESNR 和跨厂商遥测字段统一。

10. OpenROADM Overview / FAQ  
    URL: https://openroadm.org/overview/  
    URL: https://openroadm.org/faq-items/what-does-openroadm-specify/  
    用途：ROADM、Transponder、ILA 设备模型、业务模型和开放接口参考。

11. 项目汇报材料与参考论文第四章  
    用途：端云协同流程、异常子图切割、结构化事件摘要、跨模态证据对齐和伪告警排除规则。

## 使用边界

- `source` 字段应标明标准编号、来源 URL 或项目资料名称；工程化归纳条目仍需补充教材、设备手册或厂商文档页码。
- 知识块只提供可检索的物理规律、设备机理和排障依据，不直接保存实验答案或原始 telemetry。
- 历史归纳知识必须来自明确划分的训练数据，当前测试 episode 不得参与自身诊断。
- 后续扩展真实工单、设备型号和告警字典时，应同步维护来源、版本和适用范围。
