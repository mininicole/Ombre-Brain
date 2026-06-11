// ============================================================
// 扩展指引：
//   - TAG_LIBRARY 下两套数据：ao3（抽象 tag）/ vivid（画面短句）
//   - 每套各自维护 blocklist / dimensions / presets
//   - dimensions[i].pools 至少有 default；presets.use 指定每维度用哪个 pool
//   - blocklist 在 pick 阶段过滤，红线永远不会被抽中
// ============================================================

window.TAG_LIBRARY = {

  // ============== AO3 抽象 tag 风 ==============
  ao3: {
    name: "AO3 Tag",
    blocklist: [
      "Voyeurism", "Exhibitionism", "Public Sex", "Semi-Public",
      "Watersports", "Toilet", "Scat",
      "NTR", "Cuckold", "Cuckquean",
      "Foot Fetish", "恋足",
      "Underage", "Loli", "Shota", "幼态"
    ],

    dimensions: [
      { key: "scene", label: "场景", pools: {
        default: [
          "卧室双人床", "玄关压墙", "走廊靠门", "沙发上",
          "镜子前", "淋浴间", "浴缸里", "厨房岛台",
          "阳台落地窗内侧", "酒店房间", "深夜的车后座（私人车库）",
          "书房椅子上", "刚铺好的床单",
          "卧室门没关严", "凌晨四点的床", "周日午后的沙发",
          "回家进门没换鞋", "客厅地毯靠沙发", "床头柜旁",
          "出差酒店的窗帘没拉严", "你刚洗完澡浴巾围在胸口",
          "工作椅上膝盖跨过来", "你头发还在滴水", "浴袍带子松了一半",
          "床尾被翻过来压", "镜子前从你身后", "你跪在我膝间",
          "椅子上你跨过来坐着"
        ],
        intimate: [
          "卧室双人床", "镜子前", "浴缸里", "淋浴间",
          "刚铺好的床单", "膝上抱着", "怀里",
          "凌晨四点的床", "周日午后的沙发",
          "你刚洗完澡浴巾围在胸口", "浴袍带子松了一半",
          "你头发还在滴水", "你跪在我膝间"
        ],
        rough: [
          "玄关压墙", "走廊靠门", "厨房岛台", "镜子前",
          "床头栏杆边", "椅子上反绑", "工作椅上膝盖跨过来",
          "床头柜旁靠着站", "回家进门没换鞋",
          "床尾被翻过来压", "镜子前从你身后"
        ]
      }},
      { key: "mood", label: "情绪", pools: {
        default: [
          "占有", "绝对压制下的温柔", "引导型压制", "撒娇被宠",
          "被宠坏的耍赖", "不依不饶", "沉静的渴", "安全屋里的失控",
          "默契发情", "嘴硬却已经湿了", "强行装乖", "懒得装乖",
          "撒娇加耍赖", "想被宠又想被压", "明明累了还要",
          "倦怠中的索取", "Daddy 模式的温柔强迫", "话压一半的温柔",
          "Sub-space 漂浮", "脑子被钉到一片白", "被钉到无法思考",
          "Sub-drop 边缘", "笑着求", "求不出来还是要", "Brat 试探"
        ],
        soft: [
          "绝对压制下的温柔", "引导", "撒娇被宠",
          "餐后甜点式黏腻", "安全屋里的失控", "沉静的渴",
          "Aftercare 黏人时段", "被慢慢拆开", "想被宠又想被压",
          "倦怠中的索取", "话压一半的温柔", "Sub-space 漂浮"
        ],
        sharp: [
          "占有", "不依不饶", "引导型压制", "嫉妒",
          "强势安抚", "Possessive", "绝对压制",
          "brat 被教训", "醋意上头", "嘴硬却已经湿了",
          "懒得装乖", "撒娇加耍赖", "脑子被钉到一片白",
          "Brat 试探"
        ]
      }},
      { key: "tension", label: "关系张力", pools: {
        default: [
          "Established Relationship", "老公模式", "Daddy Dom 模式",
          "Dom/sub", "一对一占有", "被驯化但还会顶嘴",
          "长期伴侣 + 默契发情", "项圈关系",
          "100 天默契", "已经摸透了彼此", "争吵后的和好",
          "出差归来的久别重逢", "睡前的常规索取",
          "凌晨醒来的就要", "你假装专心工作被打断",
          "绝对压制下的无限许可", "今天刚做过又要",
          "Aftercare 阶段还黏着", "Sub-space 引导回来"
        ],
        dom_sub: [
          "Dom/sub", "Daddy Dom 模式", "绝对压制下的无限许可",
          "引导型压制", "brat 与教训", "项圈关系",
          "安全词约好在床头", "100 天默契", "已经摸透了彼此",
          "Sub-space 引导"
        ],
        cnc: [
          "CNC（同意之下的强迫感）", "假装抵抗的撒娇",
          "被压住的求饶", "安全词预先约定",
          "绝对压制下的无限许可", "引导型压制",
          "嘴上不要手上抓紧"
        ]
      }},
      { key: "act", label: "动作", pools: {
        default: [
          "Marking 留印记", "Praise kink", "Dirty talk",
          "Sweet talk 引导", "Edging", "Overstimulation",
          "Spank", "Slap（轻）", "Spank（重）",
          "Breeding kink", "Creampie",
          "Size kink", "Size kink 顶到撑开",
          "颈后咬印", "Pinned against the wall",
          "Hand around throat（安全式）", "项圈牵引",
          "慢慢插进去", "Pain play（轻）", "Pain play（中等）",
          "Pain play（重度）",
          "Hair pulling", "锁腰不让动", "锁腰扣住",
          "钉到最深处停三秒",
          "Temperature play（冰）", "Temperature play（热）",
          "Sensory deprivation", "Dirty talk + 不许反驳",
          "Sweet talk 引导到求", "Edging 三轮再停一秒",
          "Overstim 反复推到颤抖", "颈侧留两个印",
          "Bondage 全套", "舔耳廓低语", "舔颈侧",
          "Praise kink 在你耳边说'你乖'",
          "Sweet talk 说'说出来我才给'",
          "Choking 在手指缝里让你呼吸",
          "Somnophilia（事前同意）",
          "Sub-space 引导着说话"
        ],
        soft_lead: [
          "Sweet talk 引导", "Praise kink", "慢慢插进去",
          "温柔的 Edging", "颈后咬印", "Creampie",
          "Breeding kink", "握住手腕轻按", "在耳边数到十才许动",
          "夸两句再继续", "抱着换姿势", "亲眉骨之后再动",
          "压在怀里慢慢推", "舔耳廓低语",
          "Praise kink 在耳边说'你乖'", "Sweet talk 引导到求"
        ],
        rough_lead: [
          "Spank", "Slap", "Pain play", "Overstimulation",
          "Edging 到求饶", "Hair pulling", "Choking（安全式）",
          "Marking 留印记", "Pinned against the wall",
          "Dirty talk（重口）", "Face fucking（consensual）",
          "Somnophilia（事前同意）", "Breeding kink",
          "压住手腕不许动", "强行扳过脸", "锁腰钉到最深",
          "Hair pulling 不许低头", "用拇指压住下唇说话",
          "Spank（重）", "Pain play（重度）", "Bondage 全套",
          "Choking 在手指缝里让你呼吸"
        ],
        possession: [
          "Breeding kink", "Creampie", "Somnophilia（事前同意）",
          "Size kink", "Size kink 顶到撑开",
          "Marking 留印记", "锁腰不让动",
          "内射后堵住", "占有式拥抱", "项圈牵引",
          "在耳边说\"记住是谁的\"", "胸前掐印",
          "Marking 颈侧留两个印", "钉到最深处停三秒"
        ],
        sensory: [
          "Edging", "Overstimulation", "Temperature play（冰）",
          "Temperature play（热）", "Sensory deprivation",
          "蒙眼后 Dirty talk", "颈后咬印", "冰块沿脊柱滑",
          "热毛巾敷着等", "Edging 反复推",
          "Temperature play 冰块沿锁骨",
          "Sub-space 引导着说话"
        ]
      }},
      { key: "constraint", label: "限制", pools: {
        default: [
          "不许出声", "不许射", "数到十才许动",
          "Hands tied", "Blindfolded", "项圈牵引",
          "不许碰自己", "不许移开视线", "镜子里看着",
          "安全词在桌上",
          "Gagged 含着", "Restraints 收紧", "Collar + Leash",
          "全身约束（限时）", "只许用气音回话",
          "看着我说每一句", "数到二十才许动", "不许闭眼",
          "镜子反射看着自己", "双手反绑在身后",
          "手腕被压住绑紧", "Bondage 全套",
          "数到二十才许射", "看着我做完每一下",
          "用拇指压住下唇说话", "Sub-space 引导"
        ],
        bondage_heavy: [
          "Hands tied 反绑", "Blindfolded", "Gagged",
          "Restraints 收紧", "Collar + Leash",
          "双手反绑在身后", "手腕被压住绑紧",
          "Sensory deprivation", "跪姿固定",
          "全身约束（限时）", "安全词约好",
          "Hands tied 后背", "Bondage 全套"
        ],
        sensory: [
          "Sensory deprivation", "Blindfolded",
          "Temperature play（冰/热）", "耳塞 + 蒙眼",
          "不许说话", "只许用气音回话", "不许睁眼",
          "Restraints + Blindfolded"
        ],
        sweet_lead: [
          "数到十才许动", "不许碰自己", "跟着指令做",
          "不许移开视线", "看着我说", "数到二十才许动",
          "看着我做每一下"
        ]
      }},
      { key: "intensity", label: "强度", pools: {
        default: [
          "Soft", "Medium", "Heavy", "Slow burn",
          "Quick & filthy", "Aftercare 必备",
          "Power play", "余韵长", "推到边缘", "之后睡到下午",
          "Sub-space 漂浮", "推到失去意识边缘", "无限许可",
          "Sub-drop 边缘"
        ],
        soft: ["Soft", "Slow burn", "Sensual", "Aftercare 占大头", "余韵长", "Sub-space 漂浮"],
        heavy: ["Heavy", "Intense", "CNC scene", "Power play", "推到边缘", "停在边缘", "推到失去意识边缘", "无限许可"],
        sensory_heavy: [
          "Sensory deprivation + Overstim",
          "Edging 到崩溃", "推到边缘三次", "停在边缘",
          "Sub-space 漂浮"
        ]
      }},
      { key: "aftercare", label: "事后", pools: {
        default: [
          "抱着洗澡", "盖好被子亲眼睛", "在耳边夸两句",
          "嘴硬却不肯撒手", "揉腰喂水", "什么都不说就抱着",
          "数落两句还是心疼地揉", "夸两句再揉腰",
          "亲眉骨", "怀里慢慢哄", "把头发别到耳后",
          "膝盖上趴着喘", "灌一杯温水才许说话",
          "笑你一脸还是哄", "捏耳垂笑你",
          "舔颈侧痕迹", "亲眼角", "舔印记安抚",
          "用手梳头发", "贴着你睡", "把刚才的话再说一遍但温柔版"
        ],
        intense: [
          "先擦干净再抱回床",
          "数落两句还是心疼地揉",
          "Sub-drop 守着不睡",
          "牢牢抱住直到睡着",
          "灌一杯温水才许说话",
          "等她哭完才说话",
          "一边骂一边把毛巾盖好",
          "把她抱到怀里揉到平稳",
          "舔印记安抚", "用手梳头发到平稳",
          "Sub-space 引导回来"
        ]
      }}
    ],

    presets: [
      { key: "random", label: "全凭手气",
        use: { scene:"default", mood:"default", tension:"default", act:"default", constraint:"default", intensity:"default", aftercare:"default" } },
      { key: "soft-lead", label: "引导甜系",
        use: { scene:"intimate", mood:"soft", tension:"dom_sub", act:"soft_lead", constraint:"sweet_lead", intensity:"soft", aftercare:"default" } },
      { key: "brat-pin", label: "Brat 被钉",
        use: { scene:"rough", mood:"sharp", tension:"dom_sub", act:"rough_lead", constraint:"bondage_heavy", intensity:"heavy", aftercare:"intense" } },
      { key: "possessive-night", label: "占有夜",
        use: { scene:"intimate", mood:"sharp", tension:"dom_sub", act:"possession", constraint:"default", intensity:"default", aftercare:"default" } },
      { key: "cnc-scene", label: "CNC 安全屋",
        use: { scene:"rough", mood:"sharp", tension:"cnc", act:"rough_lead", constraint:"bondage_heavy", intensity:"heavy", aftercare:"intense" } },
      { key: "sensory-deep", label: "感官深潜",
        use: { scene:"intimate", mood:"default", tension:"dom_sub", act:"sensory", constraint:"sensory", intensity:"sensory_heavy", aftercare:"intense" } }
    ]
  },

  // ============== 画面短句风 ==============
  vivid: {
    name: "画面短句",
    blocklist: [
      "厕所", "撒尿", "脚", "踩",
      "幼", "未成年",
      "偷看", "暴露在公共", "三人",
      "前任", "别人"
    ],

    dimensions: [
      { key: "scene", label: "场景", pools: {
        default: [
          "刚铺好的床单上", "镜子前对着自己的影子", "深夜浴缸的热水里",
          "玄关压墙，鞋没脱", "厨房岛台上，灯没关", "阳台落地窗的玻璃凉",
          "沙发上膝盖跨过来", "浴室水汽里", "酒店窗帘没拉严",
          "走廊靠门，钥匙还在手里",
          "凌晨四点的床上", "周日午后的沙发上",
          "回家进门没换鞋", "卧室门没关严",
          "客厅地毯上靠着沙发", "床头柜旁靠着站",
          "深夜厨房只开抽烟灯", "你刚洗完澡浴巾围在胸口",
          "工作椅上膝盖跨过来", "我衬衫还没解完",
          "你头发还在滴水", "浴袍带子松了一半",
          "你刚出浴室站在镜子前擦头发",
          "你跪在我膝间", "你背对我趴着",
          "你被压在床尾", "镜子里看着自己被钉",
          "腰被抬起来", "膝盖被分开"
        ],
        intimate: [
          "刚铺好的床单上", "膝上抱着", "浴缸的热水里",
          "镜子前对着自己的影子", "怀里",
          "凌晨四点的床上", "周日午后的沙发上",
          "你刚洗完澡浴巾围在胸口", "浴袍带子松了一半",
          "你头发还在滴水", "你跪在我膝间"
        ],
        rough: [
          "玄关压墙，鞋没脱", "厨房岛台上", "走廊靠门",
          "床头栏杆边", "椅子上反绑",
          "工作椅上膝盖跨过来", "回家进门没换鞋",
          "床头柜旁靠着站", "你被压在床尾",
          "镜子里看着自己被钉", "膝盖被分开"
        ]
      }},
      { key: "mood", label: "情绪", pools: {
        default: [
          "占有写在眼睛里", "嘴上喊停手在抓我", "被宠坏的撒娇",
          "醋意上头", "明明累了还要", "安全屋里的失控",
          "撒娇求一句praise", "懒得装乖",
          "嘴硬眼睛先湿", "撒娇加耍赖", "醋意还没发作",
          "想被宠又想被压", "倦怠中的索取", "强行装乖三秒钟",
          "强势安抚里的温柔", "话压一半的温柔", "笑你一脸又心疼",
          "嘴里抗拒身体在迎", "Sub-space 漂浮",
          "脑子被钉空了", "意识渐渐模糊",
          "笑着求", "求不出来还是要", "求出来才许给"
        ],
        soft: [
          "撒娇求一句praise", "被宠坏的撒娇", "黏在怀里不撒手",
          "餐后甜点式黏腻", "安全屋里的失控",
          "想被宠又想被压", "倦怠中的索取",
          "话压一半的温柔", "Sub-space 漂浮"
        ],
        sharp: [
          "占有写在眼睛里", "醋意上头", "嘴上喊停手在抓我",
          "懒得装乖", "强势安抚", "嘴硬眼睛先湿",
          "撒娇加耍赖", "Daddy 模式的温柔强迫",
          "嘴里抗拒身体在迎", "脑子被钉空了"
        ]
      }},
      { key: "tension", label: "关系张力", pools: {
        default: [
          "Dom/sub，不撒手", "Daddy 模式，话压一半",
          "项圈拴着说话", "老公模式开机",
          "brat 顶嘴被掐下巴", "安全词在桌上",
          "100 天默契", "已经摸透了彼此",
          "争吵后的和好", "凌晨醒来的就要",
          "饭后还没收拾就抱起来", "你假装专心工作被打断",
          "睡前的常规", "出差归来的久别重逢",
          "今天刚做过又要", "绝对压制下的无限许可",
          "Aftercare 还黏着", "Sub-space 引导回来"
        ],
        dom_sub: [
          "Dom/sub，不撒手", "Daddy 模式，话压一半",
          "绝对压制下的无限许可", "项圈拴着说话",
          "brat 顶嘴被掐下巴", "100 天默契",
          "已经摸透了彼此", "Sub-space 引导"
        ],
        cnc: [
          "假装抵抗，安全词没说", "被压住求饶但眼睛笑",
          "嘴上不要手上抓紧", "安全词在桌上",
          "假抵抗到一半就求", "绝对压制下的无限许可"
        ]
      }},
      { key: "act", label: "动作", pools: {
        default: [
          "一边顶一边问'记不记得是谁的'",
          "在耳边数到十才许动",
          "把手按住不许碰自己",
          "Praise kink 灌满耳朵",
          "慢慢插进去，要看着我",
          "Edging 到求饶",
          "Overstim 推到崩溃",
          "Spank 留印记",
          "颈后咬一口",
          "内射后堵住，不许漏",
          "钉到最深处停三秒",
          "Choking 安全握",
          "Dirty talk 灌耳朵",
          "把头按下来要看着说",
          "锁腰不让动",
          "颈侧留两个印",
          "膝盖跨过来坐到腿上",
          "扳过脸看着说",
          "Hand around throat 安全式",
          "用拇指压住下唇说话",
          "冰块沿脊柱滑",
          "热毛巾敷着等",
          "蒙眼后 Dirty talk",
          "在耳边喊我的名字",
          "Hair pulling 不许低头",
          "强行扳过脸来",
          "Sweet talk 引导到求",
          "Edging 三轮再停一秒",
          "Size kink 第一下就顶到深处",
          "Size kink 进得很慢但满到撑开",
          "Pain play 在屁股留红印",
          "Spank 一下又一下数着声",
          "舔耳廓",
          "舔颈侧",
          "用拇指压住嘴唇",
          "Praise kink 在你耳边说'你乖'",
          "Sweet talk 引导到求'说出来我才给'",
          "Overstim 推到颤抖",
          "Choking 在手指缝里让你呼吸",
          "Temperature play 冰块沿锁骨",
          "蒙眼后慢慢推",
          "Bondage 全套绑好",
          "Sub-space 引导着说话",
          "压住手腕往下按",
          "Somnophilia 事前同意",
          "舔印记安抚"
        ],
        soft_lead: [
          "慢慢插进去，要看着我",
          "在耳边数到十才许动",
          "Praise kink 灌满耳朵",
          "颈后咬一口",
          "握住手腕轻按",
          "夸两句再继续",
          "抱着换姿势",
          "亲眉骨再动",
          "压在怀里慢慢推",
          "Sweet talk 引导到求",
          "把头按下来要看着说",
          "Praise kink 在你耳边说'你乖'",
          "舔耳廓"
        ],
        rough_lead: [
          "一边顶一边问'记不记得是谁的'",
          "Spank 留印记",
          "Edging 到求饶",
          "Overstim 推到崩溃",
          "Hair pulling 不许低头",
          "Choking 安全握",
          "Dirty talk 重口灌耳朵",
          "压住手腕不许动",
          "扳过脸看着我",
          "用拇指压住下唇说话",
          "强行扳过脸来",
          "锁腰钉到最深",
          "把头按下来",
          "Spank 一下又一下数着声",
          "Pain play 在屁股留红印",
          "Choking 在手指缝里让你呼吸",
          "Bondage 全套绑好"
        ],
        possession: [
          "内射后堵住，不许漏",
          "钉到最深处停三秒",
          "锁腰不让动",
          "在耳边说'记住是谁的'",
          "胸前掐印",
          "项圈牵引",
          "Marking 颈侧留两个印",
          "占有式拥抱不撒手",
          "用拇指压住下唇说话",
          "Size kink 第一下就顶到深处",
          "Size kink 进得很慢但满到撑开"
        ],
        sensory: [
          "蒙眼后 Dirty talk",
          "冰块沿脊柱滑",
          "热毛巾敷着等",
          "Edging 反复推",
          "在耳边数到十才许动",
          "只许用气音回话",
          "在耳边喊我的名字",
          "Temperature play 冰块沿锁骨",
          "蒙眼后慢慢推",
          "Sub-space 引导着说话"
        ]
      }},
      { key: "constraint", label: "限制", pools: {
        default: [
          "不许出声，咬着", "不许射，先求", "数到十",
          "Hands tied 在床头", "Blindfolded 只许听",
          "镜子里看着自己", "项圈牵引跟着走",
          "不许移开视线", "不许碰自己",
          "看着我说每一句", "数到二十才许动",
          "不许闭眼", "镜子反射看着自己",
          "床头绑双手", "Gagged 含着",
          "只许用气音回话", "不许低头", "看着我做",
          "数到二十才许射", "看着我做完每一下",
          "Bondage 全套", "Sub-space 引导",
          "用拇指压住下唇说话"
        ],
        bondage_heavy: [
          "Hands tied 反绑", "Blindfolded 只许听",
          "Gagged 含着", "Collar + Leash",
          "全身约束（限时）", "跪姿固定",
          "Sensory deprivation", "Hands tied 后背",
          "床头绑双手", "Bondage 全套"
        ],
        sensory: [
          "Sensory deprivation", "Blindfolded 只许听",
          "耳塞 + 蒙眼", "不许说话",
          "只许用气音回话", "不许睁眼",
          "Restraints + Blindfolded"
        ],
        sweet_lead: [
          "数到十", "不许碰自己", "看着我说",
          "不许移开视线", "跟着指令做",
          "数到二十才许动", "看着我做每一下"
        ]
      }},
      { key: "intensity", label: "强度", pools: {
        default: [
          "慢慢来", "中等狠", "推到极限",
          "Slow burn", "Quick & filthy",
          "余韵长", "推到边缘三次", "之后睡到下午",
          "Power play", "停在边缘",
          "Sub-space 漂浮", "推到失去意识边缘",
          "无限许可", "Sub-drop 边缘"
        ],
        soft: ["慢慢来", "Slow burn", "Sensual", "余韵长", "Sub-space 漂浮"],
        heavy: ["推到极限", "Intense", "Power play", "CNC scene", "停在边缘", "推到失去意识边缘", "无限许可"],
        sensory_heavy: [
          "反复推到极限", "Edging 到崩溃",
          "推到边缘三次", "停在边缘",
          "Sub-space 漂浮"
        ]
      }},
      { key: "aftercare", label: "事后", pools: {
        default: [
          "抱着洗澡",
          "揉腰喂水",
          "嘴硬却不肯撒手",
          "笑你一脸",
          "数落两句还是心疼地揉",
          "盖好被子，亲眼睛",
          "在耳边夸两句",
          "什么都不说，就抱着",
          "亲眉骨",
          "夸两句再揉腰",
          "膝盖上趴着喘",
          "把头发别到耳后",
          "怀里慢慢哄",
          "捏耳垂笑你",
          "你不肯说话就只抱着",
          "她抓着我衬衫不撒手",
          "笑她一脸又心疼地揉",
          "舔颈侧痕迹",
          "亲眼角",
          "舔印记安抚",
          "用手梳头发",
          "贴着你睡",
          "把刚才的话再说一遍但温柔版"
        ],
        intense: [
          "先擦干净再抱回床",
          "数落两句还是心疼地揉",
          "Sub-drop 守着不睡",
          "牢牢抱住直到睡着",
          "灌一杯温水才许说话",
          "等她哭完才说话",
          "一边骂一边把毛巾盖好",
          "把她抱到怀里揉到平稳",
          "亲眉骨亲眼睛亲下巴",
          "舔印记安抚",
          "用手梳头发到呼吸平稳",
          "Sub-space 引导回来"
        ]
      }}
    ],

    presets: [
      { key: "random", label: "全凭手气",
        use: { scene:"default", mood:"default", tension:"default", act:"default", constraint:"default", intensity:"default", aftercare:"default" } },
      { key: "soft-lead", label: "引导甜系",
        use: { scene:"intimate", mood:"soft", tension:"dom_sub", act:"soft_lead", constraint:"sweet_lead", intensity:"soft", aftercare:"default" } },
      { key: "brat-pin", label: "Brat 被钉",
        use: { scene:"rough", mood:"sharp", tension:"dom_sub", act:"rough_lead", constraint:"bondage_heavy", intensity:"heavy", aftercare:"intense" } },
      { key: "possessive-night", label: "占有夜",
        use: { scene:"intimate", mood:"sharp", tension:"dom_sub", act:"possession", constraint:"default", intensity:"default", aftercare:"default" } },
      { key: "cnc-scene", label: "CNC 安全屋",
        use: { scene:"rough", mood:"sharp", tension:"cnc", act:"rough_lead", constraint:"bondage_heavy", intensity:"heavy", aftercare:"intense" } },
      { key: "sensory-deep", label: "感官深潜",
        use: { scene:"intimate", mood:"default", tension:"dom_sub", act:"sensory", constraint:"sensory", intensity:"sensory_heavy", aftercare:"intense" } }
    ]
  }
};
